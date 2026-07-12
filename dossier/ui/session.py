"""A warm Dossier session for the UI: one browser + cache + registry kept alive
across chat turns (the `dossier interactive` pattern), so repeated questions
reuse the same session and caches.

Reuses the CLI's composition root (`_build_registry`) and calls the engine's
`run()` unchanged - the UI is just another front-end, like `cli/main.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dossier.cli.main import _build_registry
from dossier.config import SETTINGS
from dossier.engine.llm.claude import ClaudeClient
from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.search.registry import Registry
from dossier.infra.browser.fetcher import WebFetcher
from dossier.infra.browser.session import BrowserSession
from dossier.infra.cache.store import CacheStore
from dossier.infra.domaingate import DomainGate
from dossier.infra.events.emitter import EventEmitter
from dossier.sources.dictionary import DictionaryClient


def _deny_unknown(_host: str) -> str:
    """Default, non-blocking domain-gate prompt for the UI: auto-deny unknown
    hosts (seeded/known hosts are allowed without prompting). A GUI can inject a
    real approve/deny dialog callback instead."""
    return "n"


class WarmSession:
    """Async context manager holding the long-lived collaborators. Call `ask`
    per user message; it builds a fresh LLM client bound to that turn's emitter
    (so each turn gets its own cost accounting and event stream)."""

    def __init__(
        self,
        *,
        headless: bool = False,
        gate_prompt: Callable[[str], str] | None = None,
        max_iterations: int = orchestrator_loop.DEFAULT_MAX_ITERATIONS,
        max_cost_usd: float = orchestrator_loop.DEFAULT_MAX_COST_USD,
    ) -> None:
        self._headless = headless
        self._gate_prompt = gate_prompt or _deny_unknown
        self._max_iterations = max_iterations
        self._max_cost_usd = max_cost_usd
        self._cache: CacheStore | None = None
        self._browser: BrowserSession | None = None
        self._fetcher: WebFetcher | None = None
        self._dictionary: DictionaryClient | None = None
        self._registry: Registry | None = None
        self._gate: DomainGate | None = None

    async def start(self) -> None:
        self._cache = CacheStore()
        self._gate = DomainGate.load(
            SETTINGS.paths.domains_path,
            allow_seed=SETTINGS.domains.seed_allow,
            prompt=self._gate_prompt,
        )
        self._dictionary = DictionaryClient()
        self._browser = BrowserSession(SETTINGS.paths.browser_profile_dir, headless=self._headless)
        await self._browser.start()
        self._registry = _build_registry(self._cache, self._browser)
        self._fetcher = WebFetcher(self._browser, self._cache, EventEmitter("dossier-ui-session"))

    async def close(self) -> None:
        if self._fetcher is not None:
            await self._fetcher.aclose()
        if self._dictionary is not None:
            await self._dictionary.aclose()
        if self._browser is not None:
            await self._browser.stop()
        if self._cache is not None:
            self._cache.close()

    async def __aenter__(self) -> WarmSession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def ask(self, question: str, emitter: EventEmitter) -> orchestrator_loop.RunResult:
        if self._registry is None or self._fetcher is None or self._cache is None:
            raise RuntimeError("WarmSession.start() must be awaited before ask()")
        llm = ClaudeClient(self._cache, emitter)
        return await orchestrator_loop.run(
            question,
            self._registry,
            self._fetcher,
            llm,
            emitter,
            gate=self._gate,
            dictionary=self._dictionary,
            max_iterations=self._max_iterations,
            max_cost_usd=self._max_cost_usd,
        )
