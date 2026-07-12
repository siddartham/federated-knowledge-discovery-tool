"""A single event emitter routes (event, run_id, timestamp, ...fields) to the
console and to a JSON-lines run log. `dossier devtools bench/compare` parses
that run log to compute regression metrics without re-running the pipeline -
so field names on any given event type should stay stable across versions.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import structlog

# Documents the event vocabulary the rest of the app emits; not enforced, but
# keep this list in sync so devtools/bench.py has one place to look.
KNOWN_EVENTS = frozenset(
    {
        "run_start",
        "run_end",
        "input_rejected",
        "answer_redacted",
        "iteration_start",
        "iteration_complete",
        "plan_complete",
        "search_complete",
        "scrape_complete",
        "scrape_skipped",
        "lookup_complete",
        "tool_complete",
        "tool_route",
        "tool_route_fallback",
        "acronym_resolved",
        "score_complete",
        "restitch_complete",
        "synthesize_complete",
        "browser_navigated",
        "browser_crashed",
        "browser_recovery",
        "browser_console_error",
        "llm_request",
        "llm_response",
        "json_parse_retry",
        "cache_hit",
        "cache_miss",
    }
)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


class EventEmitter:
    """Emits one structured event per pipeline step: to stderr (via structlog)
    and, if `log_path` is given, appended as a JSON line to a run log."""

    def __init__(self, run_id: str, log_path: Path | None = None) -> None:
        self._run_id = run_id
        self._logger = structlog.get_logger().bind(run_id=run_id)
        self._file = open(log_path, "a", encoding="utf-8") if log_path else None
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        self._logger.info(event, **fields)
        if self._file is not None:
            payload = {"event": event, "run_id": self._run_id, **fields}
            with self._lock:
                self._file.write(json.dumps(payload, default=str) + "\n")
                self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> EventEmitter:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
