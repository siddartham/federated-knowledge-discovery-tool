"""Aggregates tools across one or more MCP clients into a single provider the
orchestrator can plan against, dispatching each call to the owning client.

Tool-call results are disk-cached (reusing the search cache) keyed on
(tool, canonical-args), so repeated calls hit cache and re-runs stay
deterministic - the same invariant the search/lookup paths rely on.
"""

from __future__ import annotations

import json
from typing import Any, cast

from dossier.infra.cache.store import SEARCH_CACHE_TTL_S, CacheStore
from dossier.sources.mcp.client import MCPClient, ToolContent, ToolSpec

_MISSING: Any = object()


class MCPToolProvider:
    def __init__(self, clients: dict[str, MCPClient], cache: CacheStore | None = None) -> None:
        self._clients = clients
        self._cache = cache
        self._catalog: list[ToolSpec] | None = None
        self._owner: dict[str, MCPClient] = {}

    async def list_tools(self) -> list[ToolSpec]:
        if self._catalog is None:
            catalog: list[ToolSpec] = []
            for client in self._clients.values():
                for spec in await client.list_tools():
                    catalog.append(spec)
                    self._owner[spec.name] = client
            self._catalog = catalog
        return self._catalog

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        await self.list_tools()  # ensures the owner map is populated
        client = self._owner.get(name)
        if client is None:
            raise KeyError(f"no MCP client owns tool {name!r}")

        key = f"tool|{name}|{json.dumps(arguments, sort_keys=True, default=str)}"
        if self._cache is not None:
            cached = self._cache.search_cache.get(key, default=_MISSING)
            if cached is not _MISSING:
                return cast(list[ToolContent], cached)

        content = await client.call_tool(name, arguments)
        if self._cache is not None:
            self._cache.search_cache.set(key, content, expire=SEARCH_CACHE_TTL_S)
        return content

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
