"""Tests for the MCP Testing Platform source, driven by a FakeMCPClient
(no `mcp` dependency, no server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dossier.engine.search.registry import Registry
from dossier.engine.search.result import Options
from dossier.infra.cache.store import CacheStore
from dossier.sources.mcp import FakeMCPClient, MCPTestingPlatformSource, ToolContent


def _search_test_runs(args: dict[str, Any]) -> list[ToolContent]:
    query = args.get("query", "")
    return [
        ToolContent(
            id="run-1042",
            title="payment-service integration suite — 2 failures",
            text=f"query={query!r}: live integration run for payment-service — "
            "2/318 failed; flaky: TxnTimeoutTest",
            uri="https://testing.acme/runs/1042",
        )
    ]


def _get_test_run(args: dict[str, Any]) -> list[ToolContent]:
    run_id = args["id"]
    return [
        ToolContent(
            id=run_id,
            title=f"test run {run_id}",
            text="component tests: 512/512 passed",
            uri=f"https://testing.acme/runs/{run_id}",
        )
    ]


def _fake() -> FakeMCPClient:
    return FakeMCPClient(
        {
            "search_test_runs": ("Search test runs by free text", _search_test_runs),
            "get_test_run": ("Fetch a single run by id", _get_test_run),
        }
    )


async def test_search_normalizes_tool_output_to_results() -> None:
    src = MCPTestingPlatformSource(_fake())
    await src.initialize()

    results = await src.search("payment-service flaky", Options(limit=5))

    assert len(results) == 1
    r = results[0]
    assert r.source == "testing_platform"
    assert r.id == "run-1042"
    assert "payment-service" in r.content
    assert r.permalink == "https://testing.acme/runs/1042"
    assert r.metadata["tool"] == "search_test_runs"


async def test_lookup_resolves_run_by_id() -> None:
    src = MCPTestingPlatformSource(_fake())
    result = await src.lookup("run-777")
    assert result is not None
    assert result.id == "run-777"
    assert result.permalink == "https://testing.acme/runs/run-777"


async def test_source_plugs_into_registry_and_caches(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    registry = Registry(cache)
    registry.register(MCPTestingPlatformSource(_fake()))

    out = await registry.search("testing_platform", "flaky payment", Options(limit=5))
    assert out and out[0].source == "testing_platform"
    # Second identical call is served from the search cache (byte-identical).
    again = await registry.search("testing_platform", "flaky payment", Options(limit=5))
    assert [r.id for r in again] == [r.id for r in out]
    cache.close()


async def test_initialize_rejects_server_without_the_search_tool() -> None:
    client = FakeMCPClient({"get_test_run": ("only lookup", _get_test_run)})
    src = MCPTestingPlatformSource(client)
    with pytest.raises(RuntimeError, match="search_test_runs"):
        await src.initialize()


async def test_unconfigured_source_errors_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOSSIER_MCP_TESTING_URL", raising=False)
    src = MCPTestingPlatformSource()  # no injected client, no env
    with pytest.raises(RuntimeError, match="DOSSIER_MCP_TESTING_URL"):
        await src.initialize()
