"""Tests for the UI engine-glue (no Chainlit import needed)."""

from __future__ import annotations

import asyncio

from dossier.ui.events import UIEventEmitter
from dossier.ui.session import _deny_unknown


async def test_ui_emitter_tees_events_to_queue() -> None:
    queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
    emitter = UIEventEmitter("t", queue)

    emitter.emit("search_complete", source="slack", result_count=3)

    event, fields = queue.get_nowait()
    assert event == "search_complete"
    assert fields == {"source": "slack", "result_count": 3}
    emitter.close()


async def test_ui_emitter_drops_events_when_queue_is_full() -> None:
    queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue(maxsize=1)
    emitter = UIEventEmitter("t", queue)

    emitter.emit("a")  # fills the queue
    emitter.emit("b")  # would block/raise on a full queue — must be dropped instead

    assert queue.qsize() == 1
    assert queue.get_nowait()[0] == "a"
    emitter.close()


def test_default_gate_prompt_denies_unknown_hosts() -> None:
    # The UI's non-blocking default must never approve an unknown host.
    assert _deny_unknown("evil.example.com") == "n"


def test_desktop_free_port_is_valid() -> None:
    from dossier.ui.desktop import _free_port

    port = _free_port()
    assert 1024 <= port <= 65535


def test_desktop_run_errors_cleanly_without_deps() -> None:
    import shutil

    import pytest

    if shutil.which("chainlit") is not None:
        pytest.skip("chainlit installed; run() would launch a real window")
    from dossier.ui.desktop import run

    with pytest.raises(SystemExit, match="desktop"):
        run()
