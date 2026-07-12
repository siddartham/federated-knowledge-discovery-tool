"""Tests for the tool gate: allowlist filtering + argument validation in
GuardedToolProvider, and the pure validate_tool_args helper."""

from __future__ import annotations

from typing import Any

import pytest

from dossier.sources.mcp import GuardedToolProvider, GuardrailViolation, ToolContent, ToolSpec
from dossier.sources.mcp.guarded import validate_tool_args

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["query"],
    "additionalProperties": False,
}


class _StubProvider:
    """Minimal ToolProvider: two tools, one read-only search and one mutating
    deploy, recording every call that actually reaches it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("search_test_runs", "find test runs", _SEARCH_SCHEMA),
            ToolSpec("deploy_release", "promote a build", {}),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        self.calls.append((name, arguments))
        return [ToolContent(id="r1", title="t", text="x")]

    async def aclose(self) -> None:
        return None


def test_validate_tool_args_rules() -> None:
    assert validate_tool_args({"query": "x"}, _SEARCH_SCHEMA) is None
    assert "missing required" in (validate_tool_args({}, _SEARCH_SCHEMA) or "")
    assert "unexpected" in (validate_tool_args({"query": "x", "evil": 1}, _SEARCH_SCHEMA) or "")
    assert "type" in (validate_tool_args({"query": 5}, _SEARCH_SCHEMA) or "")
    assert validate_tool_args({"anything": True}, {}) is None  # no schema => nothing to enforce


async def test_allowlist_hides_blocked_tools_from_planner() -> None:
    guard = GuardedToolProvider(_StubProvider(), allowlist=("search_test_runs",))
    names = [t.name for t in await guard.list_tools()]
    assert names == ["search_test_runs"]  # deploy_release never reaches the plan prompt


async def test_call_to_non_allowlisted_tool_is_refused() -> None:
    inner = _StubProvider()
    guard = GuardedToolProvider(inner, allowlist=("search_test_runs",))
    with pytest.raises(GuardrailViolation):
        await guard.call_tool("deploy_release", {})
    assert inner.calls == []  # blocked before dispatch


async def test_arg_validation_blocks_bad_calls_and_allows_good_ones() -> None:
    inner = _StubProvider()
    guard = GuardedToolProvider(inner, validate_args=True)  # empty allowlist => allow all names
    with pytest.raises(GuardrailViolation):
        await guard.call_tool("search_test_runs", {})  # missing required 'query'
    with pytest.raises(GuardrailViolation):
        await guard.call_tool("search_test_runs", {"query": "x", "evil": 1})  # unexpected key
    out = await guard.call_tool("search_test_runs", {"query": "payment-service"})
    assert out[0].id == "r1"
    assert inner.calls == [("search_test_runs", {"query": "payment-service"})]


async def test_validation_can_be_disabled() -> None:
    inner = _StubProvider()
    guard = GuardedToolProvider(inner, validate_args=False)
    await guard.call_tool("search_test_runs", {})  # no schema check -> passes through
    assert inner.calls == [("search_test_runs", {})]
