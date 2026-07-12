"""SearchSource Protocol - the pluggable interface every source under
``columbo_py.sources`` implements. Replaces the Go BaseSource interface;
``typing.Protocol`` gives structural (duck) typing so a source needs no
explicit base class, but unlike Go, Pydantic/typing validate shapes explicitly
rather than only at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from columbo_py.engine.search.result import Options, Result


@dataclass(slots=True)
class Capabilities:
    filters: tuple[str, ...] = ()
    supports_days: bool = False
    supports_author: bool = False
    supports_scope: bool = False
    max_limit: int = 100


@runtime_checkable
class SearchSource(Protocol):
    def name(self) -> str:
        """Short, stable identifier: "slack", "confluence", "github", ..."""
        ...

    def kind(self) -> str:
        """LLM-facing description of this source's query syntax and strengths.
        Interpolated directly into the orchestrator (plan) prompt so the model
        knows what a good query for this source looks like."""
        ...

    async def initialize(self) -> None:
        """Idempotent setup/auth. Called lazily by the registry on first use;
        safe to call more than once (e.g. after a credential refresh)."""
        ...

    async def search(self, query: str, opts: Options) -> list[Result]:
        ...

    def capabilities(self) -> Capabilities:
        ...


@runtime_checkable
class LookupSource(Protocol):
    """Optional capability: point lookup by exact ID/term (a Jira ticket key,
    a Confluence page title, a Slack channel name) bypassing full-text search.
    A source implements this in addition to SearchSource; the registry checks
    ``isinstance(source, LookupSource)`` before calling it."""

    async def lookup(self, term: str) -> Result | None:
        ...


@runtime_checkable
class LookupCapableSource(SearchSource, LookupSource, Protocol):
    """A SearchSource that also implements LookupSource. Combining the two
    protocols means callers get both `.name()` and `.lookup()` from a single
    isinstance check, instead of narrowing to LookupSource and losing the
    SearchSource members a static type checker would otherwise flag."""
