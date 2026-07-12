from __future__ import annotations

from pathlib import Path

import pytest

from dossier.infra.cache.store import CacheStore


def test_clear_all_namespaces(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    cache.response_cache.set("a", 1)
    cache.search_cache.set("b", 2)
    cache.lookup_cache.set("c", 3)
    cache.scrape_cache.set("d", 4)

    cleared = cache.clear()

    assert cleared == {"response": 1, "search": 1, "lookup": 1, "scrape": 1}
    assert len(cache.search_cache) == 0
    assert len(cache.response_cache) == 0
    cache.close()


def test_clear_single_namespace_leaves_others(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    cache.response_cache.set("a", 1)
    cache.search_cache.set("b", 2)

    cleared = cache.clear("search")

    assert cleared == {"search": 1}
    assert len(cache.search_cache) == 0
    # The response cache is deliberately untouched.
    assert cache.response_cache.get("a") == 1
    cache.close()


def test_clear_unknown_namespace_raises(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    with pytest.raises(ValueError, match="unknown cache namespace"):
        cache.clear("bogus")
    cache.close()
