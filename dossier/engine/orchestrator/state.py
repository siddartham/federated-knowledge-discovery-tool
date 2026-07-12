"""Orchestrator state: everything accumulated across every iteration of the
plan/execute/score loop, plus the evidence-selection logic that turns scored
raw results into the bounded list handed to the synthesis prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dossier.config import SETTINGS
from dossier.engine.orchestrator.models import MAX_SCORE, ActionStats, RestitchResult, ScoreResult
from dossier.engine.search.result import Result

# composite score cutoff for inclusion (a fraction of the 0..1 composite,
# stored as points/MAX_SCORE so it stays exact - 4/13 by default)
DEFAULT_SCORE_THRESHOLD = SETTINGS.evidence.score_threshold_points / MAX_SCORE
DEFAULT_MIN_GUARANTEE = SETTINGS.evidence.min_guarantee  # always include top-N
DEFAULT_CHAR_BUDGET = SETTINGS.evidence.char_budget  # cap on total evidence content


@dataclass(slots=True)
class SearchBatch:
    source: str
    query: str
    results: list[Result]


@dataclass(slots=True)
class ScrapeResult:
    url: str
    content: str


@dataclass(slots=True)
class ScoredEvidence:
    """One piece of evidence with its composite score attached, ready for the
    synthesis prompt. `origin` is "search" or "scrape" - useful for debugging
    which pipeline stage produced it."""

    source: str
    id: str
    title: str
    content: str
    permalink: str | None
    score: float
    origin: str


@dataclass(slots=True)
class State:
    """Accumulates across the whole run (all iterations), not just one
    fan-out phase - the orchestrator re-plans against everything gathered so
    far, not just the latest iteration's results."""

    question: str
    actions_taken: list[str] = field(default_factory=list)
    batches: list[SearchBatch] = field(default_factory=list)
    scrapes: list[ScrapeResult] = field(default_factory=list)
    dictionary: dict[str, str] = field(default_factory=dict)  # term/acronym -> definition (digest)
    dictionary_checked: set[str] = field(default_factory=set)  # acronyms already looked up (found or not)
    lookups: list[Result] = field(default_factory=list)  # full lookup hits (citable evidence)
    scored_results: dict[str, ScoreResult] = field(default_factory=dict)  # result id -> score
    scored_scrapes: dict[str, RestitchResult] = field(default_factory=dict)  # url -> restitch
    stats: ActionStats = field(default_factory=ActionStats)

    def evidence_summary(self, top_scored: int = 5) -> str:
        """Human/LLM-readable digest of everything gathered so far, for the
        next plan prompt.

        Includes a "top evidence by score" section: without it the planner
        judges confidence purely from result *counts*, decoupled from the
        per-result quality the scoring stage already computed. Surfacing the
        best composite scores lets the loop's stop decision reflect how good
        the evidence is, not just how much of it came back."""
        lines: list[str] = []
        for batch in self.batches:
            lines.append(
                f"- searched {batch.source!r} for {batch.query!r}: {len(batch.results)} results"
            )
        for scrape in self.scrapes:
            lines.append(f"- scraped {scrape.url}")
        for term, definition in self.dictionary.items():
            lines.append(f"- looked up {term!r}: {definition}")

        best = _all_candidates_sorted(self)[:top_scored]
        if best:
            lines.append("")
            lines.append(f"Top {len(best)} evidence items by score so far:")
            for e in best:
                # Surface the permalink so the planner can actually pick a URL
                # to scrape (the RULES/scrape action point at "URLs in the
                # evidence"); None-guarded since some sources have no permalink.
                loc = f"  ({e.permalink})" if e.permalink else ""
                lines.append(f"  - [{e.score:.2f}] {e.source}:{e.id} {e.title}{loc}")
        return "\n".join(lines)


def _all_candidates_sorted(state: State) -> list[ScoredEvidence]:
    """Every scored piece of evidence gathered so far, in a single
    deterministic order: (score desc, source, id). Never insertion order -
    asyncio.gather does not guarantee that across runs, and the synthesis
    prompt (and therefore the LLM response cache key) is built from this
    list, so non-determinism here breaks the whole cache."""
    candidates: list[ScoredEvidence] = []

    for batch in state.batches:
        for result in batch.results:
            score = state.scored_results.get(result.id)
            if score is None:
                continue
            candidates.append(
                ScoredEvidence(
                    source=result.source,
                    id=result.id,
                    title=result.title,
                    content=result.content,
                    permalink=result.permalink,
                    score=score.composite,
                    origin="search",
                )
            )

    for result in state.lookups:
        score = state.scored_results.get(result.id)
        if score is None:
            continue
        candidates.append(
            ScoredEvidence(
                source=result.source,
                id=result.id,
                title=result.title,
                content=result.content,
                permalink=result.permalink,
                score=score.composite,
                origin="lookup",
            )
        )

    for scrape in state.scrapes:
        restitch = state.scored_scrapes.get(scrape.url)
        # Skip: no restitch, the "nothing useful" zero-width sentinel, a
        # reversed span, or a start below 1. The model is told lines are
        # 1-indexed, but the schema permits 0 - guard so a stray 0 can't
        # produce a `lines[-1:...]` slice that grabs the wrong span.
        if (
            restitch is None
            or restitch.chunk_start < 1
            or restitch.chunk_end <= restitch.chunk_start
        ):
            continue
        lines = scrape.content.splitlines()
        snippet = "\n".join(lines[restitch.chunk_start - 1 : restitch.chunk_end])
        candidates.append(
            ScoredEvidence(
                source="web",
                id=scrape.url,
                title=scrape.url,
                content=snippet,
                permalink=scrape.url,
                score=restitch.composite,
                origin="scrape",
            )
        )

    candidates.sort(key=lambda e: (-e.score, e.source, e.id))
    return candidates


def select_evidence(
    state: State,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    min_guarantee: int = DEFAULT_MIN_GUARANTEE,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> list[ScoredEvidence]:
    """Three knobs that don't exist in a hobby RAG pipeline:

    - min_guarantee: always include the top-N by score, regardless of
      threshold - so a run with uniformly mediocre evidence still hands the
      synthesizer *something* instead of an empty context.
    - score_threshold: below the top-N, only admit evidence that scored well
      enough to be worth the tokens.
    - char_budget: hard cap on total evidence content (a token-equivalent
      proxy) - context window management, not just quality filtering.

    Candidates are pre-sorted by score descending, so once an item fails
    both the guarantee and the threshold/budget check, every remaining item
    (lower score) would too - safe to stop instead of scanning the rest.
    """
    candidates = _all_candidates_sorted(state)

    selected: list[ScoredEvidence] = []
    chars = 0
    for i, item in enumerate(candidates):
        item_chars = len(item.content)
        within_budget = item.score >= score_threshold and chars + item_chars <= char_budget
        if i < min_guarantee or within_budget:
            selected.append(item)
            chars += item_chars
        else:
            break
    return selected
