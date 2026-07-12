"""Source-agnostic result and query-option shapes shared by every SearchSource."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Options:
    """Query-shaping knobs. Not every source honors every field -
    ``SearchSource.capabilities()`` tells the orchestrator which ones apply."""

    limit: int = 20
    days: int | None = None
    author: str | None = None
    scope: str | None = None
    augment: bool = False
    """If True, the source should return richer context where cheap to do so:
    expanded threads (Slack), full page body instead of an excerpt (Confluence),
    or attachment text. Anything requiring a browser fetch stays a `scrape`
    action instead - this flag only covers augmentation the source's own API
    already gives you for free."""


@dataclass(slots=True)
class Result:
    """One hit from a source. `metadata` carries source-specific fields; the
    accessor methods below give typed access to the common ones without every
    caller needing to know each source's metadata schema."""

    source: str
    id: str
    title: str
    content: str
    timestamp: datetime | None = None
    permalink: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def channel(self) -> str | None:
        return self.metadata.get("channel")

    def repository(self) -> str | None:
        return self.metadata.get("repository")

    def space(self) -> str | None:
        return self.metadata.get("space")

    def issue_number(self) -> int | None:
        return self.metadata.get("issue_number")
