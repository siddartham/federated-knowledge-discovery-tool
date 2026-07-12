"""Parallel fan-out executor: runs every search/scrape/lookup action from one
plan step concurrently (Phase 1), then scores everything newly gathered in a
second parallel phase (Phase 2). The deterministic post-sort (Phase 3, per
the design doc) lives in `state.select_evidence` instead of here, since it
must run against the full accumulated state - not just this iteration's
fan-out - every time evidence is read.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

from pydantic import ValidationError

from dossier.config import SETTINGS
from dossier.engine.llm.client import LLMClient
from dossier.engine.llm.parsing import extract_json
from dossier.engine.orchestrator.acronyms import extract_acronyms
from dossier.engine.orchestrator.cost import CostTracker
from dossier.engine.orchestrator.models import Actions, RestitchResult, ScoreResult, ToolCall
from dossier.engine.orchestrator.state import ScrapeResult, SearchBatch, State
from dossier.engine.orchestrator.tool_provider import MAX_TOOL_CALLS, ToolProvider
from dossier.engine.prompts.render import render_prompt
from dossier.engine.search.registry import Registry
from dossier.engine.search.result import Options, Result
from dossier.infra.browser.fetcher import Fetcher
from dossier.infra.domaingate import DomainGate
from dossier.infra.events.emitter import EventEmitter
from dossier.sources.dictionary import DictionaryClient

# Tunable knobs, from config/defaults.toml ([actions], [models]).
_ACRONYM_SCAN_CHARS = SETTINGS.actions.acronym_scan_chars  # per-result content scanned for acronyms
_MAX_SENSES = SETTINGS.actions.max_senses  # dictionary senses kept per acronym in the digest
_MAX_SENSE_CHARS = SETTINGS.actions.max_sense_chars  # per-sense cap against prompt bloat

_BLOCKED_EXTENSIONS = (
    ".zip", ".exe", ".dmg", ".mp4", ".mov", ".mp3", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
)
_MAX_URL_LENGTH = SETTINGS.actions.max_url_length
_SCORE_CONTENT_PREVIEW_CHARS = SETTINGS.actions.score_content_preview_chars

SCORE_MODEL = SETTINGS.models.score
RESTITCH_MODEL = SETTINGS.models.restitch

# URLs that are never worth fetching: single-page-app "open in app" redirects,
# meeting-join links, and auth-walled SaaS consoles. They render as an empty
# shell or a login page, never the content the planner is after, so we drop
# them before they're ever queued for a fetch. Matched against the whole URL.
_URL_SKIP_PATTERNS = (
    r"//app\.slack\.com/",                       # Slack "open in app" SPA
    r"//[a-z0-9-]+\.slack\.com/archives/",       # Slack message permalinks (auth SPA)
    r"//slack\.com/app_redirect",                # Slack open-in-app dialog
    r"//([a-z0-9-]+\.)?zoom\.us/(j|my|w|wc)/",   # Zoom meeting links
    r"//teams\.microsoft\.com/l/meetup-join",    # Microsoft Teams meeting links
    r"//teams\.live\.com/meet/",                 # Teams (consumer) meeting links
    r"//meet\.google\.com/",                     # Google Meet
    r"//([a-z0-9-]+\.)?workday\.com/",           # Workday
    r"//([a-z0-9-]+\.)?myworkday\.com/",         # Workday (tenant hosts)
    r"//mail\.google\.com/",                     # Gmail
    r"//([a-z0-9-]+\.)?path\.com/",              # Path (adjust host to your org's tool)
)
_URL_SKIP_RE = re.compile("|".join(_URL_SKIP_PATTERNS), re.IGNORECASE)


def should_skip_url(url: str, already_scraped: set[str]) -> bool:
    """Guards against "URLs that explode the application": non-http(s)
    schemes, obviously-binary file extensions, absurdly long URLs (often a
    tracking/redirect chain or a hallucinated query string), known-useless
    hosts (open-in-app / meeting / auth-walled links, see `_URL_SKIP_RE`),
    and anything already scraped this run."""
    if len(url) > _MAX_URL_LENGTH:
        return True
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    if parsed.path.lower().endswith(_BLOCKED_EXTENSIONS):
        return True
    if _URL_SKIP_RE.search(url):
        return True
    return url in already_scraped


async def execute_actions(
    state: State,
    actions: Actions,
    registry: Registry,
    fetcher: Fetcher,
    llm: LLMClient,
    emitter: EventEmitter,
    *,
    cost: CostTracker | None = None,
    gate: DomainGate | None = None,
    dictionary: DictionaryClient | None = None,
    tool_provider: ToolProvider | None = None,
) -> None:
    await _run_actions(state, actions, registry, fetcher, emitter, gate, tool_provider)
    await enrich_acronyms(state, dictionary, emitter)
    await _score_new_evidence(state, llm, emitter, cost)


async def enrich_acronyms(
    state: State, dictionary: DictionaryClient | None, emitter: EventEmitter
) -> None:
    """Auto-resolve internal acronyms found in the question and in search-result
    text against the company dictionary, storing definitions in
    `state.dictionary` so they show up in the plan digest and help later
    iterations. Idempotent: each acronym is attempted at most once per run
    (tracked in `state.dictionary_checked`), so it's safe to call both before
    the first plan (question-only) and after each search round."""
    if dictionary is None or not dictionary.configured:
        return

    text_parts = [state.question]
    for batch in state.batches:
        for result in batch.results:
            text_parts.append(f"{result.title} {result.content[:_ACRONYM_SCAN_CHARS]}")
    for hit in state.lookups:
        text_parts.append(f"{hit.title} {hit.content[:_ACRONYM_SCAN_CHARS]}")

    candidates = extract_acronyms("\n".join(text_parts)) - state.dictionary_checked
    if not candidates:
        return
    state.dictionary_checked |= candidates

    ordered = sorted(candidates)  # deterministic order for cache stability

    async def resolve(acronym: str) -> None:
        try:
            senses = await dictionary.define(acronym)
        except Exception as exc:
            emitter.emit("acronym_resolved", acronym=acronym, ok=False, error=str(exc))
            return
        if senses:
            state.dictionary[acronym] = _format_senses(senses)
            emitter.emit("acronym_resolved", acronym=acronym, ok=True, senses=len(senses))

    await asyncio.gather(*(resolve(a) for a in ordered))


def _format_senses(senses: list[str]) -> str:
    """Fold one acronym's dictionary senses into a single digest string. Keeps
    them all (numbered) when there's more than one so the planner can pick the
    sense that fits the context, but bounds both count and length so a
    free-form paragraph or a long list can't dominate the plan prompt."""
    kept = [
        (s[:_MAX_SENSE_CHARS].rstrip() + "…") if len(s) > _MAX_SENSE_CHARS else s
        for s in senses[:_MAX_SENSES]
    ]
    if len(kept) == 1:
        text = kept[0]
    else:
        text = "; ".join(f"({i}) {s}" for i, s in enumerate(kept, 1))
    overflow = len(senses) - len(kept)
    if overflow > 0:
        text += f" (+{overflow} more)"
    return text


async def _run_actions(
    state: State,
    actions: Actions,
    registry: Registry,
    fetcher: Fetcher,
    emitter: EventEmitter,
    gate: DomainGate | None = None,
    tool_provider: ToolProvider | None = None,
) -> None:
    state_lock = asyncio.Lock()
    already_scraped = {s.url for s in state.scrapes}

    # Resolve scrape URLs BEFORE the parallel fan-out. The skip filter drops
    # junk/useless hosts, then the domain gate vets each remaining host (an
    # unknown one prompts once and persists the answer). Doing this serially
    # keeps interactive prompts from interleaving across concurrent tasks; an
    # aborted prompt raises ErrAborted, which propagates out to end the run.
    scrape_urls: list[str] = []
    for url in actions.scrapes:
        if should_skip_url(url, already_scraped):
            emitter.emit("scrape_skipped", url=url, reason="filtered")
            continue
        if gate is not None and not await gate.allows(url):
            emitter.emit("scrape_skipped", url=url, reason="domain_denied")
            continue
        scrape_urls.append(url)

    async def do_search(source: str, query: str) -> None:
        try:
            results = await registry.search(source, query, Options(limit=50, days=90))
        except Exception as exc:
            async with state_lock:
                state.actions_taken.append(f"search {source} {query!r} -> error: {exc}")
            emitter.emit("search_complete", source=source, query=query, ok=False, error=str(exc))
            return
        async with state_lock:
            state.batches.append(SearchBatch(source=source, query=query, results=results))
            state.actions_taken.append(f"search {source} {query!r} -> {len(results)} results")
            state.stats.searches += 1
            state.stats.results += len(results)
        emitter.emit(
            "search_complete", source=source, query=query, ok=True, result_count=len(results)
        )

    async def do_scrape(url: str) -> None:
        # URL was already vetted by the skip filter + domain gate above.
        try:
            content = await fetcher.fetch(url)
        except Exception as exc:
            async with state_lock:
                state.actions_taken.append(f"scrape {url} -> error: {exc}")
            emitter.emit("scrape_complete", url=url, ok=False, error=str(exc))
            return
        async with state_lock:
            state.scrapes.append(ScrapeResult(url=url, content=content))
            state.stats.scrapes += 1

    async def do_lookup(term: str) -> None:
        # Unlike do_search/do_scrape, this used to run unguarded inside the
        # gather below - but registry.lookup does live HTTP with
        # raise_for_status(), so a single failing lookup would propagate out
        # of the gather, cancel the sibling searches/scrapes, and crash the
        # whole iteration. Catch here to honor the "every do_* coroutine
        # catches its own exceptions" invariant the gather relies on.
        try:
            for source in registry.all_lookup_sources():
                definition = await registry.lookup(source.name(), term)
                if definition is not None:
                    async with state_lock:
                        # dictionary feeds the planner digest; lookups (the
                        # full Result) is what reaches synthesis as citable
                        # evidence, once scored in Phase 2. Dedup by id so a
                        # term the planner repeats across iterations isn't
                        # added twice.
                        state.dictionary[term] = definition.content
                        if all(r.id != definition.id for r in state.lookups):
                            state.lookups.append(definition)
                        state.stats.lookups += 1
                    emitter.emit("lookup_complete", term=term, source=source.name(), ok=True)
                    return
        except Exception as exc:
            async with state_lock:
                state.actions_taken.append(f"lookup {term!r} -> error: {exc}")
            emitter.emit("lookup_complete", term=term, source=None, ok=False, error=str(exc))
            return
        emitter.emit("lookup_complete", term=term, source=None, ok=False)

    async def do_tool_call(tc: ToolCall) -> None:
        if tool_provider is None:
            return
        try:
            content = await tool_provider.call_tool(tc.name, tc.arguments)
        except Exception as exc:
            async with state_lock:
                state.actions_taken.append(f"tool {tc.name} -> error: {exc}")
            emitter.emit("tool_complete", tool=tc.name, ok=False, error=str(exc))
            return
        # Tool output becomes an ordinary evidence batch: scored in Phase 2 and
        # synthesized like any search result. The batch's query label is the
        # canonical arguments, so it's stable for the plan digest and cache.
        query = json.dumps(tc.arguments, sort_keys=True, default=str)
        results = [
            Result(
                source=f"tool:{tc.name}",
                id=block.id,
                title=block.title,
                content=block.text,
                permalink=block.uri,
                metadata={"tool": tc.name},
            )
            for block in content
        ]
        async with state_lock:
            state.batches.append(SearchBatch(source=f"tool:{tc.name}", query=query, results=results))
            state.actions_taken.append(f"tool {tc.name} -> {len(results)} results")
            state.stats.searches += 1
            state.stats.results += len(results)
        emitter.emit("tool_complete", tool=tc.name, ok=True, result_count=len(results))

    # Phase 1: everything in parallel. return_exceptions is unnecessary here -
    # every do_* coroutine already catches its own exceptions so one failed
    # search/scrape/lookup/tool can't cancel the sibling tasks in the gather.
    await asyncio.gather(
        *(do_search(a.source, a.query) for a in actions.searches),
        *(do_scrape(u) for u in scrape_urls),
        *(do_lookup(t) for t in actions.lookups),
        *(do_tool_call(tc) for tc in actions.tool_calls[:MAX_TOOL_CALLS]),
    )


async def _score_new_evidence(
    state: State, llm: LLMClient, emitter: EventEmitter, cost: CostTracker | None = None
) -> None:
    """Phase 2: score every not-yet-scored result and scraped page, in
    parallel. Idempotent across iterations - only unscored items generate a
    new LLM call, so re-planning against evidence gathered two iterations ago
    doesn't re-score it."""

    async def score_batch(batch: SearchBatch) -> None:
        unscored = [r for r in batch.results if r.id not in state.scored_results]
        if not unscored:
            return
        system_prompt, user_prompt = render_prompt(
            "score.j2",
            question=state.question,
            results=[
                {
                    "id": r.id,
                    "title": r.title,
                    "source": r.source,
                    "content": r.content[:_SCORE_CONTENT_PREVIEW_CHARS],
                }
                for r in unscored
            ],
        )
        response = await llm.generate(
            system_prompt=system_prompt,
            prompt=user_prompt,
            model=SCORE_MODEL,
            request_type="score",
        )
        if cost is not None:
            cost.record(SCORE_MODEL, response.input_tokens, response.output_tokens)

        # The score model's JSON is exactly as unreliable as the plan model's,
        # so parse it just as defensively: extract_json tolerates code fences /
        # preamble, and per-item Pydantic validation (0-13 range, required
        # fields) means one malformed row is skipped instead of raising out of
        # the gather and crashing the whole run. A bad row leaves that result
        # unscored, which just excludes it from evidence - a safe degradation.
        try:
            raw_scores = extract_json(response.text)
        except json.JSONDecodeError:
            emitter.emit(
                "score_complete", batch_source=batch.source, ok=False, error="invalid_json"
            )
            return
        if not isinstance(raw_scores, list):
            emitter.emit(
                "score_complete", batch_source=batch.source, ok=False, error="not_a_list"
            )
            return
        scored = 0
        # Log each result's four dimensions (not just the composite) so the
        # scoring design can be validated offline - e.g. a dimension-correlation
        # check that the four axes are actually independent. See
        # docs/scoring-and-confidence.md ("Validating the design").
        scores_log: list[dict[str, object]] = []
        for result, raw in zip(unscored, raw_scores, strict=False):
            try:
                score = ScoreResult.model_validate(
                    {**raw, "source": result.id} if isinstance(raw, dict) else raw
                )
            except (ValidationError, TypeError):
                continue
            state.scored_results[result.id] = score
            scored += 1
            scores_log.append(
                {
                    "id": result.id,
                    "direct_relevance": score.direct_relevance,
                    "answer_potential": score.answer_potential,
                    "context_value": score.context_value,
                    "source_quality": score.source_quality,
                    "composite": round(score.composite, 4),
                }
            )
        emitter.emit(
            "score_complete",
            batch_source=batch.source,
            ok=True,
            scored=scored,
            expected=len(unscored),
            scores=scores_log,
        )

    async def restitch_scrape(scrape: ScrapeResult) -> None:
        if scrape.url in state.scored_scrapes:
            return
        lines = scrape.content.splitlines()
        system_prompt, user_prompt = render_prompt(
            "restitch.j2", question=state.question, url=scrape.url, lines=lines
        )
        response = await llm.generate(
            system_prompt=system_prompt,
            prompt=user_prompt,
            model=RESTITCH_MODEL,
            request_type="restitch",
        )
        if cost is not None:
            cost.record(RESTITCH_MODEL, response.input_tokens, response.output_tokens)
        try:
            result = RestitchResult.model_validate(extract_json(response.text))
        except (json.JSONDecodeError, TypeError, ValidationError):
            emitter.emit(
                "scrape_complete", url=scrape.url, ok=False, error="invalid_restitch_json"
            )
            return
        state.scored_scrapes[scrape.url] = result
        # Same four dimensions as the search scorer, logged so restitch-scored
        # scrape evidence joins the dimension-correlation analysis (devtools analyze).
        emitter.emit(
            "restitch_complete",
            url=scrape.url,
            direct_relevance=result.direct_relevance,
            answer_potential=result.answer_potential,
            context_value=result.context_value,
            source_quality=result.source_quality,
            composite=round(result.composite, 4),
        )

    # Lookup hits are scored through the same scorer as search results (a
    # synthetic batch), so they get real direct_relevance scores and flow into
    # evidence selection instead of only informing the planner digest.
    # score_batch filters by id, so already-scored lookups are skipped.
    lookup_batches = (
        [SearchBatch(source="lookup", query="lookup", results=state.lookups)]
        if state.lookups
        else []
    )

    # Phase 2: score everything in parallel.
    await asyncio.gather(
        *(score_batch(b) for b in state.batches),
        *(score_batch(b) for b in lookup_batches),
        *(restitch_scrape(s) for s in state.scrapes),
    )
