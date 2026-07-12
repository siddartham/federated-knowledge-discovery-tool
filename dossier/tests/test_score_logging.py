"""The run log persists the per-dimension scores (not just the composites), so the
scoring design can be validated offline. See docs/scoring-and-confidence.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dossier.engine.llm.mock import MockLLMClient
from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.orchestrator.actions import execute_actions
from dossier.engine.orchestrator.cost import CostTracker
from dossier.engine.orchestrator.models import Actions, ToolCall
from dossier.engine.orchestrator.state import State
from dossier.engine.search.registry import Registry
from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.infra.cache.store import CacheStore
from dossier.infra.events.emitter import EventEmitter
from dossier.sources.mcp import FakeMCPClient, MCPToolProvider, ToolContent

_CONF_KEYS = (
    "explicit_evidence",
    "implicit_evidence",
    "evidence_consistency",
    "answer_specificity",
)


class _CaptureEmitter(EventEmitter):
    """Records emitted (event, fields) instead of writing to a file/stderr."""

    def __init__(self) -> None:
        super().__init__("test")
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


class _NullFetcher:
    async def fetch(self, url: str) -> str:
        return ""


def _testing_client() -> FakeMCPClient:
    def search_test_runs(_args: dict[str, Any]) -> list[ToolContent]:
        return [ToolContent(id="run-1042", title="t", text="x")]

    return FakeMCPClient({"search_test_runs": ("search test runs", search_test_runs)})


async def test_score_complete_logs_per_dimension_scores(tmp_path: Path) -> None:
    provider = MCPToolProvider({"testing_platform": _testing_client()})
    state = State(question="q")
    actions = Actions(tool_calls=[ToolCall(name="search_test_runs", arguments={"query": "x"})])
    score_json = json.dumps(
        [{"source": "run-1042", "direct_relevance": 13, "answer_potential": 8,
          "context_value": 5, "source_quality": 3}]
    )
    emitter = _CaptureEmitter()

    await execute_actions(
        state, actions, Registry(CacheStore(tmp_path)), _NullFetcher(),
        MockLLMClient([score_json]), emitter, cost=CostTracker(1.0), tool_provider=provider,
    )

    ok_events = [f for e, f in emitter.events if e == "score_complete" and f.get("ok")]
    assert ok_events, "no successful score_complete event"
    scores = ok_events[0]["scores"]
    assert scores[0]["id"] == "run-1042"
    # all four dimensions, not just the composite, are on the wire
    assert scores[0]["direct_relevance"] == 13
    assert scores[0]["answer_potential"] == 8
    assert scores[0]["context_value"] == 5
    assert scores[0]["source_quality"] == 3
    assert 0.0 <= scores[0]["composite"] <= 1.0


class _OneDocSource:
    async def initialize(self) -> None:
        return None

    def name(self) -> str:
        return "demo"

    def kind(self) -> str:
        return "in-memory demo source"

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def search(self, query: str, opts: Options) -> list[Result]:
        return [Result(source="demo", id="1", title="t", content="Dossier answers questions.",
                       permalink="demo://1")]


async def test_run_result_carries_selected_contexts(tmp_path: Path) -> None:
    plan = json.dumps({"thinking": "t", "confidence": {k: 3 for k in _CONF_KEYS},
                       "actions": {"searches": [{"source": "demo", "query": "q"}],
                                   "scrapes": [], "lookups": []}})
    score = json.dumps([{"source": "1", "direct_relevance": 13, "answer_potential": 8,
                         "context_value": 5, "source_quality": 8}])
    plan2 = json.dumps({"thinking": "done", "confidence": {k: 13 for k in _CONF_KEYS},
                        "actions": {"searches": [], "scrapes": [], "lookups": []}})
    synth = json.dumps({"answer": "answer [demo:1]",
                        "citations": [{"source": "demo", "id": "1", "permalink": "demo://1"}]})
    registry = Registry(CacheStore(tmp_path))
    registry.register(_OneDocSource())

    result = await orchestrator_loop.run(
        "q", registry, _NullFetcher(),
        MockLLMClient([plan, score, plan2, synth]), EventEmitter("t"),
    )

    assert result.contexts, "run captured no selected contexts"
    assert result.contexts[0]["id"] == "demo:1"
    assert "Dossier" in result.contexts[0]["content"]


async def test_plan_complete_logs_confidence_dimensions(tmp_path: Path) -> None:
    plan_json = json.dumps(
        {
            "thinking": "t",
            "confidence": {"explicit_evidence": 13, "implicit_evidence": 8,
                           "evidence_consistency": 5, "answer_specificity": 3},
            "actions": {"searches": [], "scrapes": [], "lookups": []},
        }
    )
    synth_json = json.dumps({"answer": "a", "citations": []})
    emitter = _CaptureEmitter()

    await orchestrator_loop.run(
        "q", Registry(CacheStore(tmp_path)), _NullFetcher(),
        MockLLMClient([plan_json, synth_json]), emitter,
    )

    plan_events = [f for e, f in emitter.events if e == "plan_complete"]
    assert plan_events, "no plan_complete event"
    for key in _CONF_KEYS:
        assert key in plan_events[0], f"plan_complete missing {key}"
    assert plan_events[0]["explicit_evidence"] == 13
    assert plan_events[0]["answer_specificity"] == 3
