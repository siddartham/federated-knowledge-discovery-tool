"""Four independently-evicted on-disk caches, one per pipeline stage:
response (LLM), search, lookup, scrape. Splitting by component instead of one
cache with prefixed keys means `rm -rf ~/.columbo/cache/response` busts only
the LLM cache without losing search/scrape history, or vice versa.

Critical invariant (from the design doc): cache keys must be byte-identical
across runs to hit. Concurrent fan-out (asyncio.gather over searches/scrapes/
lookups) does not guarantee result ordering, so anything derived from a batch
of results - especially LLM prompts built from them - must be sorted into a
canonical order *before* it's used as part of a cache key. See
``engine.orchestrator.state`` for where that sort happens.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import diskcache

from columbo_py.config import SETTINGS

DEFAULT_CACHE_ROOT = SETTINGS.cache.dir

# Freshness TTLs (seconds) for the content caches. The response cache is
# deliberately excluded - it stays permanent so identical (question, state)
# re-runs stay byte-for-byte deterministic and never re-hit the API. But
# search/lookup/scrape results go stale (new messages, edited docs, closed
# tickets), so without an expiry a cached query would shadow fresh source
# data forever. These bound how stale a cache hit can be; configured in
# config/defaults.toml ([cache]) and overridable via COLUMBO_*_TTL_S env vars.
SEARCH_CACHE_TTL_S = SETTINGS.cache.search_ttl_s
LOOKUP_CACHE_TTL_S = SETTINGS.cache.lookup_ttl_s
SCRAPE_CACHE_TTL_S = SETTINGS.cache.scrape_ttl_s

# Query params that vary per-click but don't change page content - stripped so
# the same article fetched via two different links still hits one cache entry.
_TRACKING_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query_pairs = [
        (k, v)
        for k, v in (p.split("=", 1) if "=" in p else (p, "") for p in parts.query.split("&") if p)
        if k not in _TRACKING_PARAMS
    ]
    query = "&".join(f"{k}={v}" if v else k for k, v in sorted(query_pairs))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def prompt_cache_key(system_prompt: str, user_prompt: str, model: str) -> str:
    digest = hashlib.sha256(f"{model}\n{system_prompt}\n{user_prompt}".encode()).hexdigest()
    return f"v1:{digest}"


CACHE_NAMESPACES = ("response", "search", "lookup", "scrape")


class CacheStore:
    def __init__(self, root: Path | None = None, *, scope: str | None = None) -> None:
        root = root or DEFAULT_CACHE_ROOT
        if scope:
            # Multi-tenant guardrail: partition every namespace under a
            # per-principal subdir so one user's cached search/scrape/lookup/tool
            # CONTENT can never be served to another user who issues an identical
            # query. Single-user CLI passes no scope -> the shared root, exactly
            # as before. Hashed so a raw principal id (e.g. an email) is never
            # written to disk as a directory name.
            root = root / "scopes" / hashlib.sha256(scope.encode()).hexdigest()[:16]
        root.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self.root = root
        self.response_cache = diskcache.Cache(str(root / "response"))
        self.search_cache = diskcache.Cache(str(root / "search"))
        self.lookup_cache = diskcache.Cache(str(root / "lookup"))
        self.scrape_cache = diskcache.Cache(str(root / "scrape"))

    def _caches(self) -> dict[str, diskcache.Cache]:
        return {
            "response": self.response_cache,
            "search": self.search_cache,
            "lookup": self.lookup_cache,
            "scrape": self.scrape_cache,
        }

    def clear(self, namespace: str | None = None) -> dict[str, int]:
        """Evict cached entries and return the count removed per namespace.
        With no namespace, clears all four; otherwise just the named one.
        Raises ValueError for an unknown namespace."""
        caches = self._caches()
        if namespace is not None:
            if namespace not in caches:
                raise ValueError(
                    f"unknown cache namespace {namespace!r}; "
                    f"choose from {', '.join(CACHE_NAMESPACES)}"
                )
            caches = {namespace: caches[namespace]}
        return {name: cache.clear() for name, cache in caches.items()}

    def close(self) -> None:
        for cache in (self.response_cache, self.search_cache, self.lookup_cache, self.scrape_cache):
            cache.close()

    def __enter__(self) -> CacheStore:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
