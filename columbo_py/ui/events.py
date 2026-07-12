"""An EventEmitter that also tees each pipeline event onto an asyncio.Queue so a
UI can render live "thinking" progress - without touching the engine.

The engine only knows the `EventEmitter` interface; this subclass preserves the
base behaviour (stderr + optional JSONL run log via `super().emit`) and simply
publishes a copy of each event to a queue the UI drains. If the UI falls behind,
progress events are dropped rather than blocking the engine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from columbo_py.infra.events.emitter import EventEmitter

UIEvent = tuple[str, dict[str, Any]]


class UIEventEmitter(EventEmitter):
    def __init__(
        self,
        run_id: str,
        queue: "asyncio.Queue[UIEvent]",
        log_path: Path | None = None,
    ) -> None:
        super().__init__(run_id, log_path)
        self._queue = queue

    def emit(self, event: str, **fields: Any) -> None:
        super().emit(event, **fields)
        try:
            self._queue.put_nowait((event, fields))
        except asyncio.QueueFull:
            # UI is lagging; drop the progress event rather than stall the run.
            pass
