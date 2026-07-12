"""Tests for the MCP tool-call path: selector, provider caching, and the
end-to-end flow where a planned tool_call becomes scored evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from columbo_py.engine.llm.mock import MockLLMClient
from columbo_py.engine.orchestrator.actions import execute_actions
from columbo_py.engine.orchestrator.cost import CostTracker
from columbo_py.engine.orchestrator.models import Actions, ToolCall
from columbo_py.engine.orchestrator.state import State
from columbo_py.engine.orchestrator.tool_provider import select_tools, select_tools_semantic
from columbo_py.engine.search.registry import Registry
from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.events.emitter import EventEmitter
from columbo_py.sources.mcp import FakeMCPClient, MCPToolProvider, ToolContent, ToolSpec


class _NullFetcher:
    async def fetch(self, url: str) -> str:
        return ""


def _testing_client() -> FakeMCPClient:
    def search_test_runs(args: dict[str, Any]) -> list[ToolContent]:
        return [
            ToolContent(
                id="run-1042",
                title="payment-service integration — 2 failures",
                text=f"query={args.get('query')!r}: 2/318 failed; flaky TxnTimeoutTest",
                uri="https://testing.acme/runs/1042",
            )
        ]

    return FakeMCPClient({"search_test_runs": ("search test runs", search_test_runs)})


def test_select_tools_ranks_by_overlap_and_caps() -> None:
    catalog = [
        ToolSpec("search_test_runs", "find test runs and failures"),
        ToolSpec("get_flaky_tests", "flaky test detection for a service"),
        ToolSpec("deploy_release", "unrelated: promote a build to prod"),
    ]
    picks = select_tools("which tests are flaky for payment-service", catalog, k=2)
    assert len(picks) == 2
    assert "get_flaky_tests" in [t.name for t in picks]  # 'flaky'/'test' overlap wins
    # Deterministic: same question -> same shortlist.
    assert select_tools("which tests are flaky for payment-service", catalog, k=2) == picks


def test_select_tools_returns_all_when_catalog_fits() -> None:
    catalog = [ToolSpec("a", "x"), ToolSpec("b", "y")]
    assert select_tools("anything", catalog, k=12) == catalog


async def test_semantic_router_uses_model_pick_and_filters_unknowns() -> None:
    catalog = [ToolSpec(f"tool_{i}", f"does thing {i}") for i in range(20)]
    # Router names two real tools plus a hallucinated one; the ghost is dropped,
    # order preserved.
    llm = MockLLMClient([json.dumps(["tool_3", "ghost", "tool_7"])])
    picks = await select_tools_semantic("q", catalog, llm, k=5)
    assert [t.name for t in picks] == ["tool_3", "tool_7"]
    assert llm.calls[0]["request_type"] == "route"


async def test_semantic_router_noop_when_catalog_fits() -> None:
    catalog = [ToolSpec("a", "x"), ToolSpec("b", "y")]
    llm = MockLLMClient([])  # would raise if the router called it
    picks = await select_tools_semantic("q", catalog, llm, k=12)
    assert picks == catalog
    assert llm.calls == []  # no LLM call when the catalog already fits


async def test_semantic_router_falls_back_to_lexical_on_bad_reply() -> None:
    catalog = [ToolSpec(f"tool_{i}", f"thing {i}") for i in range(20)]
    llm = MockLLMClient(["not json at all"])
    picks = await select_tools_semantic("q", catalog, llm, k=5)
    # Degraded to the deterministic lexical selection instead of vanishing.
    assert picks == select_tools("q", catalog, 5)
    assert len(picks) == 5


async def test_provider_caches_tool_calls(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_args: dict[str, Any]) -> list[ToolContent]:
        calls["n"] += 1
        return [ToolContent(id="r1", title="t", text="x")]

    provider = MCPToolProvider({"p": FakeMCPClient({"t1": ("d", handler)})}, CacheStore(tmp_path))
    first = await provider.call_tool("t1", {"q": 1})
    second = await provider.call_tool("t1", {"q": 1})
    assert first == second
    assert calls["n"] == 1  # second call served from the cache


async def test_tool_call_becomes_scored_evidence(tmp_path: Path) -> None:
    provider = MCPToolProvider({"testing_platform": _testing_client()})
    state = State(question="why did payment-service integration tests fail?")
    actions = Actions(
        tool_calls=[ToolCall(name="search_test_runs", arguments={"query": "payment-service"})]
    )
    score_json = json.dumps(
        [
            {
                "source": "run-1042",
                "direct_relevance": 13,
                "answer_potential": 13,
                "context_value": 8,
                "source_quality": 8,
            }
        ]
    )
    llm = MockLLMClient([score_json])

    await execute_actions(
        state,
        actions,
        Registry(CacheStore(tmp_path)),
        _NullFetcher(),
        llm,
        EventEmitter("t"),
        cost=CostTracker(1.0),
        tool_provider=provider,
    )

    tool_batches = [b for b in state.batches if b.source == "tool:search_test_runs"]
    assert tool_batches, "tool call did not produce an evidence batch"
    assert tool_batches[0].results[0].id == "run-1042"
    assert "run-1042" in state.scored_results  # phase-2 scoring ran on the tool output


async def test_tool_call_noop_without_provider(tmp_path: Path) -> None:
    # tool_calls present but no provider wired -> silently skipped, no crash.
    state = State(question="q")
    actions = Actions(tool_calls=[ToolCall(name="whatever", arguments={})])
    await execute_actions(
        state,
        actions,
        Registry(CacheStore(tmp_path)),
        _NullFetcher(),
        MockLLMClient([]),
        EventEmitter("t"),
        cost=CostTracker(1.0),
        tool_provider=None,
    )
    assert not state.batches
