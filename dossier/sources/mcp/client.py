"""MCP client abstraction for Dossier sources.

An MCP server exposes *tools* (name + description + input JSON schema) that a
client lists and calls. This module defines the small surface Dossier needs -
`list_tools` / `call_tool` normalized to plain dataclasses - plus two
implementations:

- `RemoteMCPClient`: a real client over streamable HTTP, using the official
  `mcp` SDK (lazily imported; needs the `mcp` extra). Carries a per-user bearer
  token so the downstream platform enforces the *user's* RBAC.
- `FakeMCPClient`: an in-memory client whose tools are Python callables, for
  tests and offline demos - no `mcp` dependency, no network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool advertised by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContent:
    """One normalized content block returned from a tool call."""

    id: str
    title: str
    text: str
    uri: str | None = None


@runtime_checkable
class MCPClient(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]: ...
    async def aclose(self) -> None: ...


# name -> (description, handler(arguments) -> content)
FakeTool = tuple[str, Callable[[dict[str, Any]], list[ToolContent]]]


class FakeMCPClient:
    """In-memory MCP client backed by Python callables. Used by tests and the
    offline demo so the whole path runs with no server and no `mcp` dependency."""

    def __init__(self, tools: dict[str, FakeTool]) -> None:
        self._tools = tools

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name=name, description=desc) for name, (desc, _) in self._tools.items()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name!r}")
        _, handler = self._tools[name]
        return handler(arguments)

    async def aclose(self) -> None:
        return None


def _content_from_block(index: int, block: Any) -> ToolContent:
    """Map an mcp content block (text/resource/...) to a ToolContent."""
    text = getattr(block, "text", "") or ""
    uri = getattr(block, "uri", None)
    title = getattr(block, "title", None) or f"result {index + 1}"
    return ToolContent(id=str(index), title=str(title), text=str(text), uri=str(uri) if uri else None)


class RemoteMCPClient:
    """Real MCP client over streamable HTTP. Requires `pip install ".[mcp]"`.
    Holds the session open across calls (closed via `aclose`). Not exercised in
    the test suite - the FakeMCPClient covers the source adapter end to end."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self._url = url
        self._token = token
        self._stack: Any = None
        self._session: Any = None

    async def connect(self) -> None:
        from contextlib import AsyncExitStack

        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - requires the mcp extra
            raise RuntimeError("MCP support requires the 'mcp' extra: pip install '.[mcp]'") from exc

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        self._stack = AsyncExitStack()
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self._url, headers=headers)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def list_tools(self) -> list[ToolSpec]:
        resp = await self._session.list_tools()
        return [
            ToolSpec(t.name, t.description or "", dict(getattr(t, "inputSchema", {}) or {}))
            for t in resp.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        resp = await self._session.call_tool(name, arguments)
        return [_content_from_block(i, block) for i, block in enumerate(resp.content)]

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None
