"""Testing Platform source, backed by an MCP server.

The Testing Platform runs offloaded test workloads (live integration,
performance, and component tests) that used to live in Jenkins/OnePipeline
stages, and exposes them via an MCP server. This adapter presents that server
as an ordinary Dossier `SearchSource`, so it plugs into the existing planner /
scoring / synthesis loop with no engine changes:

- `search(query)`  -> the server's `search_test_runs` tool (free-text)
- `lookup(id)`     -> the server's `get_test_run` tool (exact run id)

Its tool results are normalized to `Result` objects, so downstream scoring and
synthesis never learn the evidence came from an MCP tool.

Config (env): DOSSIER_MCP_TESTING_URL (server URL; unset => source disabled),
DOSSIER_MCP_TESTING_TOKEN (optional per-user bearer token for RBAC at source).
Or inject an `MCPClient` directly (tests use FakeMCPClient).
"""

from __future__ import annotations

import os

from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.sources.guidance import load_guidance
from dossier.sources.mcp.client import MCPClient

KIND_DESCRIPTION = load_guidance(__file__)
_SOURCE_NAME = "testing_platform"
_SEARCH_TOOL = "search_test_runs"
_LOOKUP_TOOL = "get_test_run"


class MCPTestingPlatformSource:
    def __init__(self, client: MCPClient | None = None, *, search_tool: str = _SEARCH_TOOL) -> None:
        self._client = client
        self._search_tool = search_tool
        self._tool_names: set[str] = set()

    def name(self) -> str:
        return _SOURCE_NAME

    def kind(self) -> str:
        return KIND_DESCRIPTION

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=(), supports_days=True, supports_author=False, supports_scope=True, max_limit=50
        )

    async def initialize(self) -> None:
        if self._client is None:
            url = os.environ.get("DOSSIER_MCP_TESTING_URL")
            if not url:
                raise RuntimeError(
                    "Testing Platform MCP not configured: set DOSSIER_MCP_TESTING_URL to the "
                    "server URL (and DOSSIER_MCP_TESTING_TOKEN for a per-user bearer token)."
                )
            from dossier.sources.mcp.client import RemoteMCPClient

            client = RemoteMCPClient(url, token=os.environ.get("DOSSIER_MCP_TESTING_TOKEN"))
            await client.connect()
            self._client = client
        self._tool_names = {tool.name for tool in await self._client.list_tools()}
        if self._search_tool not in self._tool_names:
            raise RuntimeError(
                f"Testing Platform MCP server exposes no {self._search_tool!r} tool; "
                f"available tools: {sorted(self._tool_names)}"
            )

    async def _ensure_ready(self) -> MCPClient:
        if self._client is None or not self._tool_names:
            await self.initialize()
        assert self._client is not None
        return self._client

    async def search(self, query: str, opts: Options) -> list[Result]:
        client = await self._ensure_ready()
        content = await client.call_tool(self._search_tool, {"query": query, "limit": opts.limit})
        return [
            Result(
                source=_SOURCE_NAME,
                id=block.id,
                title=block.title,
                content=block.text,
                permalink=block.uri,
                metadata={"tool": self._search_tool},
            )
            for block in content
        ]

    async def lookup(self, term: str) -> Result | None:
        """Resolve an exact test-run id via `get_test_run`; None if unavailable."""
        client = await self._ensure_ready()
        if _LOOKUP_TOOL not in self._tool_names:
            return None
        content = await client.call_tool(_LOOKUP_TOOL, {"id": term})
        if not content:
            return None
        block = content[0]
        return Result(
            source=_SOURCE_NAME,
            id=block.id,
            title=block.title,
            content=block.text,
            permalink=block.uri,
            metadata={"tool": _LOOKUP_TOOL},
        )
