from __future__ import annotations

import json

from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.llm.mock import MockLLMClient
from dossier.engine.search.registry import Registry
from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.infra.cache.store import CacheStore
from dossier.infra.events.emitter import EventEmitter


class _EchoSource:
    def name(self) -> str:
        return "demo"

    def kind(self) -> str:
        return "demo source"

    async def initialize(self) -> None:
        pass

    async def search(self, query: str, opts: Options) -> list[Result]:
        return [Result(source="demo", id="1", title="T", content="relevant content")]

    def capabilities(self) -> Capabilities:
        return Capabilities()


class _NullFetcher:
    async def fetch(self, url: str) -> str:
        return ""


def _plan(confidence: int, with_search: bool) -> str:
    return json.dumps(
        {
            "thinking": "thinking",
            "confidence": {
                "explicit_evidence": confidence,
                "implicit_evidence": confidence,
                "evidence_consistency": confidence,
                "answer_specificity": confidence,
            },
            "actions": {
                "searches": [{"source": "demo", "query": "q"}] if with_search else [],
                "scrapes": [],
                "lookups": [],
            },
        }
    )


_SCORE = json.dumps(
    [{"source": "1", "direct_relevance": 13, "answer_potential": 13, "context_value": 13, "source_quality": 13}]
)
_SYNTH = json.dumps({"answer": "the answer [demo:1]", "citations": [{"source": "demo", "id": "1"}]})


async def test_loop_terminates_on_confidence_reached(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_EchoSource())
    llm = MockLLMClient([_plan(13, with_search=False), _SYNTH])

    result = await orchestrator_loop.run("q", registry, _NullFetcher(), llm, emitter)

    assert result.terminated_reason == "confidence_reached"
    assert result.iterations == 1


async def test_loop_terminates_on_no_actions_proposed(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_EchoSource())
    # Low confidence but no actions proposed - should stop instead of looping.
    llm = MockLLMClient([_plan(2, with_search=False), _SYNTH])

    result = await orchestrator_loop.run("q", registry, _NullFetcher(), llm, emitter)

    assert result.terminated_reason == "no_actions_proposed"
    assert result.iterations == 1


async def test_loop_terminates_on_max_iterations(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_EchoSource())
    # Every plan call proposes a search and stays low-confidence forever.
    # _EchoSource always returns the same result id, so scoring only ever
    # happens once (execute_actions skips already-scored results) - only the
    # first iteration's search triggers a score call.
    llm = MockLLMClient(
        [
            _plan(2, with_search=True),
            _SCORE,
            _plan(2, with_search=True),
            _plan(2, with_search=True),
            _SYNTH,
        ]
    )

    result = await orchestrator_loop.run(
        "q", registry, _NullFetcher(), llm, emitter, max_iterations=3
    )

    assert result.terminated_reason == "max_iterations"
    assert result.iterations == 3


async def test_loop_terminates_on_cost_budget_exceeded(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_EchoSource())
    # max_cost_usd=0.0 trips the cost check right after the first plan call,
    # before execute_actions ever runs - so only one plan call happens.
    llm = MockLLMClient([_plan(2, with_search=True), _SYNTH])

    result = await orchestrator_loop.run(
        "q", registry, _NullFetcher(), llm, emitter, max_cost_usd=0.0
    )

    assert result.terminated_reason == "cost_budget_exceeded"


async def test_loop_aborts_cleanly_on_domain_gate_abort(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # A plan that proposes a scrape of an unknown host, with a gate that
    # aborts, must end the run cleanly (terminated_reason="aborted") and still
    # synthesize - not crash.
    from dossier.infra.domaingate import DomainGate

    registry = Registry(cache)
    registry.register(_EchoSource())
    plan_with_scrape = json.dumps(
        {
            "thinking": "t",
            "confidence": {k: 2 for k in (
                "explicit_evidence", "implicit_evidence", "evidence_consistency", "answer_specificity",
            )},
            "actions": {"searches": [], "scrapes": ["https://unknown.example/doc"], "lookups": []},
        }
    )
    llm = MockLLMClient([plan_with_scrape, _SYNTH])
    gate = DomainGate(prompt=lambda h: "a")

    result = await orchestrator_loop.run(
        "q", registry, _NullFetcher(), llm, emitter, gate=gate
    )

    assert result.terminated_reason == "aborted"
    assert result.answer == "the answer [demo:1]"


async def test_loop_result_includes_synthesized_answer(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_EchoSource())
    llm = MockLLMClient([_plan(13, with_search=False), _SYNTH])

    result = await orchestrator_loop.run("q", registry, _NullFetcher(), llm, emitter)

    assert result.answer == "the answer [demo:1]"
    assert result.citations == [{"source": "demo", "id": "1", "permalink": None}]


async def test_generate_validated_reprompts_on_malformed_json() -> None:
    import pytest
    from pydantic import BaseModel

    from dossier.engine.orchestrator.cost import CostTracker

    class _M(BaseModel):
        x: int

    # First reply reproduces the reported "Expecting ':' delimiter" break; the
    # helper reprompts and the second (valid) reply parses.
    llm = MockLLMClient(['{"x" 5}', '{"x": 5}'])
    result = await orchestrator_loop._generate_validated(
        llm, _M, system_prompt="s", user_prompt="u",
        model="claude-haiku-4-5", request_type="test",
        cost=CostTracker(1.0), emitter=EventEmitter("t"),
    )
    assert result.x == 5
    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] != llm.calls[0]["prompt"]  # reprompt differs

    # A shape that parses but fails validation also triggers a reprompt.
    llm2 = MockLLMClient(['{"x": "nope"}', '{"x": 7}'])
    result2 = await orchestrator_loop._generate_validated(
        llm2, _M, system_prompt="s", user_prompt="u",
        model="claude-haiku-4-5", request_type="test",
        cost=CostTracker(1.0), emitter=EventEmitter("t"),
    )
    assert result2.x == 7
    assert len(llm2.calls) == 2

    # If it never recovers, the original error propagates (after the retry).
    llm3 = MockLLMClient(["{broken", "still broken"])
    with pytest.raises(json.JSONDecodeError):
        await orchestrator_loop._generate_validated(
            llm3, _M, system_prompt="s", user_prompt="u",
            model="claude-haiku-4-5", request_type="test",
            cost=CostTracker(1.0), emitter=EventEmitter("t"),
        )
    assert len(llm3.calls) == 2
