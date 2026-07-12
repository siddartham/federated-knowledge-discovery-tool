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

from dataclasses import dataclass

from columbo_py.config import SETTINGS
from columbo_py.engine.llm.client import LLMClient
from columbo_py.engine.llm.parsing import extract_json
from columbo_py.engine.orchestrator.actions import enrich_acronyms, execute_actions
from columbo_py.engine.orchestrator.cost import CostTracker
from columbo_py.engine.orchestrator.linkify import linkify_citations, render_sources_footer
from columbo_py.engine.orchestrator.models import OrchestrationResponse, SynthesisResponse
from columbo_py.engine.orchestrator.state import (
    DEFAULT_CHAR_BUDGET,
    DEFAULT_MIN_GUARANTEE,
    DEFAULT_SCORE_THRESHOLD,
    State,
    select_evidence,
)
from columbo_py.engine.prompts.plan_config import (
    DEFAULT_PLAN_CONFIG,
    PlanConfig,
    plan_prompt_context,
)
from columbo_py.engine.prompts.render import render_prompt
from columbo_py.engine.search.registry import Registry
from columbo_py.infra.browser.fetcher import Fetcher
from columbo_py.infra.domaingate import DomainGate, ErrAborted
from columbo_py.infra.events.emitter import EventEmitter
from columbo_py.sources.dictionary import DictionaryClient

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


async def run(
    question: str,
    registry: Registry,
    fetcher: Fetcher,
    llm: LLMClient,
    emitter: EventEmitter,
    *,
    gate: DomainGate | None = None,
    dictionary: DictionaryClient | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    confidence_cutoff: float = CONFIDENCE_CUTOFF,
    plan_config: PlanConfig = DEFAULT_PLAN_CONFIG,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    min_guarantee: int = DEFAULT_MIN_GUARANTEE,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> RunResult:
    state = State(question=question)
    cost = CostTracker(max_cost_usd)
    emitter.emit(
        "run_start", question=question, max_iterations=max_iterations, max_cost_usd=max_cost_usd
    )

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
            iteration, max_iterations, plan_config,
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
                cost=cost, gate=gate, dictionary=dictionary,
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
    )


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
) -> OrchestrationResponse:
    system_prompt, user_prompt = render_prompt(
        "orchestrate.j2",
        sources_block=registry.sources_prompt_block(),
        question=state.question,
        iteration=iteration,
        max_iterations=max_iterations,
        confidence_cutoff=confidence_cutoff,
        evidence_summary=state.evidence_summary(),
        **plan_prompt_context(plan_config),
    )
    response = await llm.generate(
        system_prompt=system_prompt, prompt=user_prompt, model=PLAN_MODEL, request_type="plan"
    )
    cost.record(PLAN_MODEL, response.input_tokens, response.output_tokens)
    parsed = OrchestrationResponse.model_validate(extract_json(response.text))
    emitter.emit(
        "plan_complete",
        iteration=iteration,
        confidence=parsed.confidence.composite,
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
    response = await llm.generate(
        system_prompt=system_prompt,
        prompt=user_prompt,
        model=SYNTHESIS_MODEL,
        request_type="synthesize",
    )
    cost.record(SYNTHESIS_MODEL, response.input_tokens, response.output_tokens)
    parsed = SynthesisResponse.model_validate(extract_json(response.text))

    # Turn the model's inline [source:id] tokens into clickable links, and add
    # a Sources footer - deterministically, from the evidence permalink map, so
    # the URLs are always correct and the output stays cache-stable.
    permalinks = {(e.source, e.id): e.permalink for e in evidence if e.permalink}
    parsed.answer = linkify_citations(parsed.answer, permalinks) + render_sources_footer(
        parsed.citations, permalinks
    )

    emitter.emit(
        "synthesize_complete",
        evidence_count=len(evidence),
        citation_count=len(parsed.citations),
        linked_citations=sum(1 for c in parsed.citations if (c.source, c.id) in permalinks),
    )
    return parsed
