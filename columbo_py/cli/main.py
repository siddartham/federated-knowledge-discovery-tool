"""Columbo CLI: a command-line agent/assistant for an enterprise knowledge
base spread across multiple SaaS sources. `columbo ask "<question>"` runs
the orchestrator loop once and prints a cited answer; `columbo interactive`
keeps one browser session + cache warm across multiple questions so repeat
questions hit the same caches.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markdown import Markdown

from columbo_py.cli import devtools
from columbo_py.config import SETTINGS
from columbo_py.engine.llm.claude import ClaudeClient
from columbo_py.engine.orchestrator import loop as orchestrator_loop
from columbo_py.engine.search.registry import Registry
from columbo_py.infra.browser.fetcher import WebFetcher
from columbo_py.infra.browser.session import BrowserSession
from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.domaingate import DEFAULT_DOMAINS_PATH, DomainGate
from columbo_py.infra.events.emitter import EventEmitter, configure_logging
from columbo_py.sources.dictionary import DictionaryClient
from columbo_py.sources.confluence import ConfluenceSource
from columbo_py.sources.drive import DriveSource
from columbo_py.sources.github import GitHubCodeSource, GitHubIssuesSource
from columbo_py.sources.jira import JiraSource
from columbo_py.sources.slack import SlackSource

app = typer.Typer(
    help="Columbo: ask questions across your team's Slack, Confluence, GitHub, Drive, and Jira."
)
app.add_typer(devtools.app, name="devtools", help="compare/bench/smoke/demo dev tools")

console = Console()

DEFAULT_USER_DATA_DIR = SETTINGS.paths.browser_profile_dir
DEFAULT_RUNS_DIR = SETTINGS.paths.runs_dir

# Hosts pre-approved for scraping so the domain gate never prompts for the
# sources we already query directly. Anything else is an unknown host and gets
# a one-time y/N/abort prompt (persisted to ~/.columbo/domains.json). Seed list
# is config/defaults.toml ([domains].seed_allow).
_SEED_ALLOW_DOMAINS = SETTINGS.domains.seed_allow


def _build_registry(cache: CacheStore, browser_session: BrowserSession) -> Registry:
    """Registers every source unconditionally - sources raise a clear
    RuntimeError from `initialize()` (lazy, on first use) if their env vars
    aren't set, rather than failing at startup for sources the user isn't
    even going to query this run."""
    registry = Registry(cache)
    registry.register(GitHubCodeSource())
    registry.register(GitHubIssuesSource())
    registry.register(SlackSource(browser_session=browser_session))
    registry.register(ConfluenceSource())
    registry.register(JiraSource())
    registry.register(DriveSource())
    return registry


def _run_id_for(question: str) -> str:
    return f"run-{abs(hash(question)) % 10_000_000:07d}"


async def _ask(
    question: str, *, headless: bool, max_iterations: int, max_cost_usd: float
) -> orchestrator_loop.RunResult:
    configure_logging()
    cache = CacheStore()
    log_path = DEFAULT_RUNS_DIR / f"{_run_id_for(question)}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    emitter = EventEmitter(_run_id_for(question), log_path)

    gate = DomainGate.load(DEFAULT_DOMAINS_PATH, allow_seed=_SEED_ALLOW_DOMAINS)
    dictionary = DictionaryClient()

    async with BrowserSession(DEFAULT_USER_DATA_DIR, headless=headless) as session:
        fetcher = WebFetcher(session, cache, emitter)
        registry = _build_registry(cache, session)
        llm = ClaudeClient(cache, emitter)
        try:
            return await orchestrator_loop.run(
                question,
                registry,
                fetcher,
                llm,
                emitter,
                gate=gate,
                dictionary=dictionary,
                max_iterations=max_iterations,
                max_cost_usd=max_cost_usd,
            )
        finally:
            await fetcher.aclose()
            await dictionary.aclose()
            emitter.close()
            cache.close()


def _print_result(result: orchestrator_loop.RunResult) -> None:
    console.print(Markdown(result.answer))
    console.print(
        f"\n[dim]{result.iterations} iteration(s), confidence {result.final_confidence:.2f}, "
        f"terminated: {result.terminated_reason}, est. cost ${result.cost_usd:.4f}[/dim]"
    )


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to answer"),
    headless: bool = typer.Option(
        False, help="Run the browser headless (won't show login prompts)"
    ),
    max_iterations: int = typer.Option(
        orchestrator_loop.DEFAULT_MAX_ITERATIONS, help="Max plan/execute iterations"
    ),
    max_cost_usd: float = typer.Option(
        orchestrator_loop.DEFAULT_MAX_COST_USD, help="Stop early if estimated spend exceeds this"
    ),
) -> None:
    """Ask Columbo a question once and print the cited answer."""
    result = asyncio.run(
        _ask(question, headless=headless, max_iterations=max_iterations, max_cost_usd=max_cost_usd)
    )
    _print_result(result)


@app.command()
def interactive(
    headless: bool = typer.Option(False, help="Run the browser headless"),
    max_iterations: int = typer.Option(orchestrator_loop.DEFAULT_MAX_ITERATIONS),
    max_cost_usd: float = typer.Option(orchestrator_loop.DEFAULT_MAX_COST_USD),
) -> None:
    """Keep one browser session and cache warm across multiple questions."""
    asyncio.run(
        _interactive(headless=headless, max_iterations=max_iterations, max_cost_usd=max_cost_usd)
    )


async def _interactive(*, headless: bool, max_iterations: int, max_cost_usd: float) -> None:
    configure_logging()
    cache = CacheStore()
    # One gate for the whole session, so a domain approved for the first
    # question isn't re-prompted for the next.
    gate = DomainGate.load(DEFAULT_DOMAINS_PATH, allow_seed=_SEED_ALLOW_DOMAINS)
    dictionary = DictionaryClient()
    async with BrowserSession(DEFAULT_USER_DATA_DIR, headless=headless) as session:
        registry = _build_registry(cache, session)
        setup_emitter = EventEmitter("interactive-session")
        fetcher = WebFetcher(session, cache, setup_emitter)
        console.print("[bold]Columbo interactive mode[/bold] - Ctrl+D or 'exit' to quit.\n")
        try:
            while True:
                try:
                    question = typer.prompt("columbo>")
                except (EOFError, KeyboardInterrupt):
                    break
                if question.strip().lower() in {"exit", "quit"}:
                    break

                log_path = DEFAULT_RUNS_DIR / f"{_run_id_for(question)}.jsonl"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                emitter = EventEmitter(_run_id_for(question), log_path)
                llm = ClaudeClient(cache, emitter)
                result = await orchestrator_loop.run(
                    question,
                    registry,
                    fetcher,
                    llm,
                    emitter,
                    gate=gate,
                    dictionary=dictionary,
                    max_iterations=max_iterations,
                    max_cost_usd=max_cost_usd,
                )
                _print_result(result)
                console.print()
                emitter.close()
        finally:
            await fetcher.aclose()
            await dictionary.aclose()
            cache.close()


if __name__ == "__main__":
    app()
