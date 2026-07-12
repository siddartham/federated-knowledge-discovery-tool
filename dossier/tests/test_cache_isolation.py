"""Tests for the multi-tenant cache guardrail: a per-principal scope partitions
every namespace so cached content can't leak across identities."""

from __future__ import annotations

from pathlib import Path

from dossier.infra.cache.store import CacheStore


def test_different_scopes_do_not_share_content(tmp_path: Path) -> None:
    alice = CacheStore(tmp_path, scope="alice@corp")
    bob = CacheStore(tmp_path, scope="bob@corp")
    try:
        alice.search_cache.set("q|salary-bands", ["alice-only secret"])
        assert bob.search_cache.get("q|salary-bands") is None  # bob can't see alice's cache
        assert alice.search_cache.get("q|salary-bands") == ["alice-only secret"]
    finally:
        alice.close()
        bob.close()


def test_same_scope_shares_across_instances(tmp_path: Path) -> None:
    first = CacheStore(tmp_path, scope="alice@corp")
    first.search_cache.set("k", 1)
    first.close()
    again = CacheStore(tmp_path, scope="alice@corp")
    try:
        assert again.search_cache.get("k") == 1  # same principal -> same partition
    finally:
        again.close()


def test_no_scope_uses_shared_root_and_is_isolated_from_scoped(tmp_path: Path) -> None:
    shared = CacheStore(tmp_path)  # single-user CLI: no scope, root unchanged
    scoped = CacheStore(tmp_path, scope="alice@corp")
    try:
        assert shared.root == tmp_path
        assert scoped.root != tmp_path and scoped.root.is_relative_to(tmp_path)
        shared.search_cache.set("k", "shared")
        assert scoped.search_cache.get("k") is None  # scoped partition is separate on disk
    finally:
        shared.close()
        scoped.close()
