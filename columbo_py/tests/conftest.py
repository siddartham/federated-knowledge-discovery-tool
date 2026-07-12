from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.events.emitter import EventEmitter


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[CacheStore]:
    store = CacheStore(tmp_path / "cache")
    yield store
    store.close()


@pytest.fixture
def emitter() -> EventEmitter:
    return EventEmitter("test-run")
