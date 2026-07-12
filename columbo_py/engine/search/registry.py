"""Registry: holds every configured SearchSource, lazily initializes them, and
caches search/lookup results on disk so identical (question, state) pairs hit
cache instead of re-querying live sources. The app's determinism guarantee
("identical question w/ identical state should hit caches") lives here.
"""

from __future__ import annotations

from typing import Any, cast

from columbo_py.engine.search.result import Options, Result
from columbo_py.engine.search.source import LookupCapableSource, SearchSource
from columbo_py.infra.cache.store import LOOKUP_CACHE_TTL_S, SEARCH_CACHE_TTL_S, CacheStore

_NOT_FOUND_SENTINEL = "__COLUMBO_NOT_FOUND__"
_MISSING: Any = object()  # in-process only default for cache.get(); never stored


def _search_cache_key(source: str, query: str, opts: Options) -> str:
    return f"{source}|{query}|limit={opts.limit}|days={opts.days}"


def _lookup_cache_key(source: str, term: str) -> str:
    return f"{source}/{term}"


class Registry:
    def __init__(self, cache: CacheStore) -> None:
        self._sources: dict[str, SearchSource] = {}
        self._initialized: set[str] = set()
        self._cache = cache

    def register(self, source: SearchSource) -> None:
        self._sources[source.name()] = source

    def get(self, name: str) -> SearchSource | None:
        return self._sources.get(name)

    def all(self) -> list[SearchSource]:
        return list(self._sources.values())

    def all_lookup_sources(self) -> list[LookupCapableSource]:
        return [s for s in self._sources.values() if isinstance(s, LookupCapableSource)]

    def sources_prompt_block(self) -> str:
        """Renders each source's full query guidance, keyed by the exact
        `source` string the planner must put in a search action, for
        interpolation into the orchestrator (plan) prompt. `kind()` returns
        the source's GUIDANCE.md contents (query language, qualifiers,
        strengths)."""
        return "\n\n".join(
            f'### source key: "{s.name()}"\n{s.kind()}' for s in self._sources.values()
        )

    async def _ensure_initialized(self, source: SearchSource) -> None:
        if source.name() not in self._initialized:
            await source.initialize()
            self._initialized.add(source.name())

    async def search(self, source_name: str, query: str, opts: Options) -> list[Result]:
        source = self._sources.get(source_name)
        if source is None:
            raise KeyError(f"unknown search source: {source_name!r}")

        cache_key = _search_cache_key(source_name, query, opts)
        cached = self._cache.search_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cast(list[Result], cached)

        await self._ensure_initialized(source)
        results = await source.search(query, opts)
        self._cache.search_cache.set(cache_key, results, expire=SEARCH_CACHE_TTL_S)
        return results

    async def lookup(self, source_name: str, term: str) -> Result | None:
        source = self._sources.get(source_name)
        if source is None or not isinstance(source, LookupCapableSource):
            return None

        cache_key = _lookup_cache_key(source_name, term)
        cached = self._cache.lookup_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return None if cached == _NOT_FOUND_SENTINEL else cached

        await self._ensure_initialized(source)
        result = await source.lookup(term)
        self._cache.lookup_cache.set(
            cache_key,
            result if result is not None else _NOT_FOUND_SENTINEL,
            expire=LOOKUP_CACHE_TTL_S,
        )
        return result
