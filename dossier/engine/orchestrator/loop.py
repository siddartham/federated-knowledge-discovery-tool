"""The orchestrator loop: plan -> execute -> score -> re-plan (which doubles
as the "assess" step - a fresh plan call naturally re-evaluates confidence
against the evidence gathered since the last one) until confidence clears the
threshold, the iteration cap is hit, or the cost budget runs out. One final
synthesis call turns the accumulated evidence into a cited answer.

Self-terminating by design: the spec explicitly rules out a system that just
runs until someone kills it, so every exit path (confidence reached, no
actions left to take, cost exceeded, iteration cap) is a named, logged
reason - never a silent timeout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from dossier.config import SETTINGS
from dossier.engine.llm.client import LLMClient
from dossier.engine.llm.parsing import extract_json
from dossier.engine.orchestrator.actions import enrich_acronyms, execute_actions
from dossier.engine.orchestrator.cost import CostTracker
from dossier.engine.orchestrator.linkify import linkify_citations, render_sources_footer
from dossier.engine.orchestrator.models import OrchestrationResponse, SynthesisResponse
from dossier.engine.orchestrator.state import (
    DEFAULT_CHAR_BUDGET,
    DEFAULT_MIN_GUARANTEE,
    DEFAULT_SCORE_THRESHOLD,
    State,
    select_evidence,
)
from dossier.engine.orchestrator.tool_provider import (
    MAX_TOOL_CALLS,
    TOOL_TOP_K,
    ToolProvider,
    select_tools_semantic,
)
from dossier.engine.prompts.plan_config import (
    DEFAULT_PLAN_CONFIG,
    PlanConfig,
    plan_prompt_context,
)
from dossier.engine.prompts.render import render_prompt
from dossier.engine.search.registry import Registry
from dossier.infra.browser.fetcher import Fetcher
from dossier.infra.domaingate import DomainGate, ErrAborted
from dossier.infra.events.emitter import EventEmitter
from dossier.infra.redaction import redact_secrets
from dossier.sources.dictionary import DictionaryClient

PLAN_MODEL = SETTINGS.models.plan
SYNTHESIS_MODEL = SETTINGS.models.synthesis

CONFIDENCE_CUTOFF = SETTINGS.loop.confidence_cutoff
DEFAULT_MAX_ITERATIONS = SETTINGS.loop.max_iterations
DEFAULT_MAX_COST_USD = SETTINGS.loop.max_cost_usd


@dataclass(slots=True)
class RunResult:
    answer: str
    citations: list[dict[str, str | None]]
    iterations: int
    final_confidence: float
    terminated_reason: str
    cost_usd: float
    # The evidence selected into synthesis (id + content), so a run can be captured
    # as an eval sample (`ask --capture-eval`) for offline judging / A-B testing.
    contexts: list[dict[str, str]] = field(default_factory=list)


async def run(
    question: str,
    registry: Registry,
    fetcher: Fetcher,
    llm: LLMClient,
    emitter: EventEmitter,
    *,
    gate: DomainGate | None = None,
    dictionary: DictionaryClient | None = None,
    tool_provider: ToolProvider | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    confidence_cutoff: float = CONFIDENCE_CUTOFF,
    plan_config: PlanConfig = DEFAULT_PLAN_CONFIG,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    min_guarantee: int = DEFAULT_MIN_GUARANTEE,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> RunResult:
    # Input filter (question boundary). Redact secrets before the question is
    # ever written to the run log, and reject an oversized question before
    # spending a single LLM call.
    redacted_question, _ = redact_secrets(question)
    emitter.emit(
        "run_start",
        question=redacted_question,
        max_iterations=max_iterations,
        max_cost_usd=max_cost_usd,
    )
    max_chars = SETTINGS.guardrails.max_question_chars
    if len(question) > max_chars:
        emitter.emit("input_rejected", reason="too_long", length=len(question), limit=max_chars)
        return RunResult(
            answer=(
                f"I can't process this request: the question exceeds the {max_chars}-character "
                "limit. Please shorten it and try again."
            ),
            citations=[],
            iterations=0,
            final_confidence=0.0,
            terminated_reason="input_rejected",
            cost_usd=0.0,
        )

    state = State(question=question)
    cost = CostTracker(max_cost_usd)

    # Resolve acronyms in the QUESTION itself before the first plan, so the
    # opening plan prompt already carries their definitions instead of leaving
    # the planner to guess. Later search rounds enrich again (idempotently) from
    # result text; question acronyms resolved here won't be re-queried.
    await enrich_acronyms(state, dictionary, emitter)

    confidence = 0.0
    iteration = 0
    terminated_reason = "max_iterations"

    while iteration < max_iterations:
        iteration += 1
        emitter.emit("iteration_start", iteration=iteration)

        plan = await _plan(
            state, registry, llm, emitter, cost, confidence_cutoff,
            iteration, max_iterations, plan_config, tool_provider,
        )
        confidence = plan.confidence.composite

        if confidence >= confidence_cutoff:
            terminated_reason = "confidence_reached"
            emitter.emit(
                "iteration_complete", iteration=iteration, confidence=confidence, terminated=True
            )
            break

        if cost.exceeded():
            terminated_reason = "cost_budget_exceeded"
            emitter.emit(
                "iteration_complete", iteration=iteration, confidence=confidence, terminated=True
            )
            break

        has_actions = bool(
            plan.actions.searches or plan.actions.scrapes or plan.actions.lookups
        )
        if not has_actions:
            # The model didn't clear the confidence bar but also proposed
            # nothing new to gather - another iteration would just repeat
            # this same plan call against unchanged evidence. Stop instead
            # of looping until max_iterations for no reason.
            terminated_reason = "no_actions_proposed"
            emitter.emit(
                "iteration_complete", iteration=iteration, confidence=confidence, terminated=True
            )
            break

        try:
            await execute_actions(
                state, plan.actions, registry, fetcher, llm, emitter,
                cost=cost, gate=gate, dictionary=dictionary, tool_provider=tool_provider,
            )
        except ErrAborted:
            # User declined a domain-gate prompt with "abort": end the run
            # cleanly and synthesize from whatever was gathered before this
            # iteration, rather than crashing mid-fan-out.
            terminated_reason = "aborted"
            emitter.emit(
                "iteration_complete", iteration=iteration, confidence=confidence, terminated=True
            )
            break
        emitter.emit(
            "iteration_complete", iteration=iteration, confidence=confidence, terminated=False
        )

    answer = await _synthesize(state, llm, emitter, cost, score_threshold, min_guarantee, char_budget)

    # Recompute the selected evidence (pure, deterministic - no LLM call) so the
    # RunResult can carry the exact contexts synthesis saw.
    selected = select_evidence(
        state, score_threshold=score_threshold, min_guarantee=min_guarantee, char_budget=char_budget
    )

    emitter.emit(
        "run_end",
        iterations=iteration,
        final_confidence=confidence,
        terminated_reason=terminated_reason,
        cost_usd=round(cost.spent_usd, 4),
    )

    return RunResult(
        answer=answer.answer,
        citations=[c.model_dump() for c in answer.citations],
        iterations=iteration,
        final_confidence=confidence,
        terminated_reason=terminated_reason,
        cost_usd=cost.spent_usd,
        contexts=[{"id": f"{e.source}:{e.id}", "content": e.content} for e in selected],
    )


_JsonModel = TypeVar("_JsonModel", bound=BaseModel)


async def _generate_validated(
    llm: LLMClient,
    model_cls: type[_JsonModel],
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    request_type: str,
    cost: CostTracker,
    emitter: EventEmitter,
    retries: int = 1,
) -> _JsonModel:
    """Generate an LLM reply and parse+validate it into `model_cls`.

    Chat models occasionally emit malformed JSON (an unescaped quote, a dropped
    delimiter) or a shape that fails validation. Rather than crash the whole run
    on one bad reply, reprompt once - quoting the error and demanding a single
    bare JSON object - then give up. The reprompt text is deterministic (built
    from the same user_prompt + the error message), so the response cache stays
    byte-stable across re-runs.
    """
    last_exc: Exception | None = None
    prompt = user_prompt
    for attempt in range(retries + 1):
        response = await llm.generate(
            system_prompt=system_prompt, prompt=prompt, model=model, request_type=request_type
        )
        cost.record(model, response.input_tokens, response.output_tokens)
        try:
            return model_cls.model_validate(extract_json(response.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_exc = exc
            emitter.emit(
                "json_parse_retry",
                request_type=request_type,
                attempt=attempt + 1,
                error=str(exc)[:200],
            )
            prompt = (
                user_prompt
                + "\n\nIMPORTANT: your previous reply could not be parsed as the required JSON "
                + f"({type(exc).__name__}). Respond with ONLY a single valid JSON object matching "
                + "the schema above - no prose, no markdown, no code fences."
            )
    assert last_exc is not None  # loop always runs >= 1 time
    raise last_exc


async def _plan(
    state: State,
    registry: Registry,
    llm: LLMClient,
    emitter: EventEmitter,
    cost: CostTracker,
    confidence_cutoff: float,
    iteration: int,
    max_iterations: int,
    plan_config: PlanConfig = DEFAULT_PLAN_CONFIG,
    tool_provider: ToolProvider | None = None,
) -> OrchestrationResponse:
    tools_ctx: list[dict[str, object]] = []
    if tool_provider is not None:
        catalog = await tool_provider.list_tools()
        selected = await select_tools_semantic(
            state.question, catalog, llm, k=TOOL_TOP_K, cost=cost, emitter=emitter
        )
        tools_ctx = [
            {"name": t.name, "description": t.description, "schema": t.input_schema}
            for t in selected
        ]
    system_prompt, user_prompt = render_prompt(
        "orchestrate.j2",
        sources_block=registry.sources_prompt_block(),
        question=state.question,
        iteration=iteration,
        max_iterations=max_iterations,
        confidence_cutoff=confidence_cutoff,
        evidence_summary=state.evidence_summary(),
        tools=tools_ctx,
        max_tool_calls=MAX_TOOL_CALLS,
        **plan_prompt_context(plan_config),
    )
    parsed = await _generate_validated(
        llm,
        OrchestrationResponse,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=PLAN_MODEL,
        request_type="plan",
        cost=cost,
        emitter=emitter,
    )
    emitter.emit(
        "plan_complete",
        iteration=iteration,
        confidence=parsed.confidence.composite,
        # The four dimensions behind the composite, logged so the 0.8 cutoff can
        # be calibrated against real answer quality offline (see
        # docs/scoring-and-confidence.md, "Validating the design").
        explicit_evidence=parsed.confidence.explicit_evidence,
        implicit_evidence=parsed.confidence.implicit_evidence,
        evidence_consistency=parsed.confidence.evidence_consistency,
        answer_specificity=parsed.confidence.answer_specificity,
        thinking=parsed.thinking,
    )
    return parsed


async def _synthesize(
    state: State,
    llm: LLMClient,
    emitter: EventEmitter,
    cost: CostTracker,
    score_threshold: float,
    min_guarantee: int,
    char_budget: int,
) -> SynthesisResponse:
    evidence = select_evidence(
        state, score_threshold=score_threshold, min_guarantee=min_guarantee, char_budget=char_budget
    )
    system_prompt, user_prompt = render_prompt(
        "synthesize.j2",
        question=state.question,
        evidence=[
            {
                "source": e.source,
                "id": e.id,
                "title": e.title,
                "content": e.content,
                "permalink": e.permalink,
                "score": e.score,
            }
            for e in evidence
        ],
    )
    parsed = await _generate_validated(
        llm,
        SynthesisResponse,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=SYNTHESIS_MODEL,
        request_type="synthesize",
        cost=cost,
        emitter=emitter,
    )

    # Turn the model's inline [source:id] tokens into clickable links, and add
    # a Sources footer - deterministically, from the evidence permalink map, so
    # the URLs are always correct and the output stays cache-stable.
    permalinks = {(e.source, e.id): e.permalink for e in evidence if e.permalink}
    parsed.answer = linkify_citations(parsed.answer, permalinks) + render_sources_footer(
        parsed.citations, permalinks
    )

    # Output guard: redact any secret that leaked from evidence into the answer
    # (e.g. an API key checked into a repo the search surfaced).
    if SETTINGS.guardrails.redact_answers:
        parsed.answer, redacted = redact_secrets(parsed.answer)
        if redacted:
            emitter.emit("answer_redacted", count=redacted)

    emitter.emit(
        "synthesize_complete",
        evidence_count=len(evidence),
        citation_count=len(parsed.citations),
        linked_citations=sum(1 for c in parsed.citations if (c.source, c.id) in permalinks),
        # Which evidence ids the answer actually cited, so `devtools analyze` can
        # measure whether the scorer's ranking predicts what synthesis uses.
        cited=[{"source": c.source, "id": c.id} for c in parsed.citations],
    )
    return parsed
