"""Dossier CLI: a command-line agent/assistant for an enterprise knowledge
base spread across multiple SaaS sources. `dossier ask "<question>"` runs
the orchestrator loop once and prints a cited answer; `dossier interactive`
keeps one browser session + cache warm across multiple questions so repeat
questions hit the same caches.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown

from dossier.cli import devtools
from dossier.config import SETTINGS
from dossier.engine.llm.claude import ClaudeClient
from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.orchestrator.tool_provider import ToolProvider
from dossier.engine.search.registry import Registry
from dossier.infra.browser.fetcher import WebFetcher
from dossier.infra.browser.session import BrowserSession
from dossier.infra.cache.store import CacheStore
from dossier.infra.domaingate import DEFAULT_DOMAINS_PATH, DomainGate
from dossier.infra.events.emitter import EventEmitter, configure_logging
from dossier.sources.dictionary import DictionaryClient
from dossier.sources.confluence import ConfluenceSource
from dossier.sources.drive import DriveSource
from dossier.sources.github import GitHubCodeSource, GitHubIssuesSource
from dossier.sources.jira import JiraSource
from dossier.sources.slack import SlackSource

app = typer.Typer(
    help="Dossier: ask questions across your team's Slack, Confluence, GitHub, Drive, and Jira."
)
app.add_typer(devtools.app, name="devtools", help="compare/bench/smoke/demo dev tools")

console = Console()

DEFAULT_USER_DATA_DIR = SETTINGS.paths.browser_profile_dir
DEFAULT_RUNS_DIR = SETTINGS.paths.runs_dir

# Hosts pre-approved for scraping so the domain gate never prompts for the
# sources we already query directly. Anything else is an unknown host and gets
# a one-time y/N/abort prompt (persisted to ~/.dossier/domains.json). Seed list
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


async def _build_tool_provider(cache: CacheStore) -> ToolProvider | None:
    """Opt-in MCP tool provider: built only when DOSSIER_MCP_TESTING_URL is set,
    so the default plan prompt / behaviour is unchanged for users without it.
    The token is sent as a per-user bearer so the platform enforces the caller's
    RBAC. Needs the `mcp` extra (pip install '.[mcp]'). The raw provider is
    wrapped in a GuardedToolProvider so the [guardrails] policy (tool allowlist +
    argument validation) applies before any tool runs."""
    url = os.environ.get("DOSSIER_MCP_TESTING_URL")
    if not url:
        return None
    from dossier.sources.mcp import GuardedToolProvider, MCPToolProvider, RemoteMCPClient

    client = RemoteMCPClient(url, token=os.environ.get("DOSSIER_MCP_TESTING_TOKEN"))
    await client.connect()
    provider = MCPToolProvider({"testing_platform": client}, cache)
    policy = SETTINGS.guardrails
    return GuardedToolProvider(
        provider,
        allowlist=policy.tool_allowlist,
        validate_args=policy.validate_tool_args,
    )


def _run_id_for(question: str) -> str:
    return f"run-{abs(hash(question)) % 10_000_000:07d}"


async def _ask(
    question: str, *, headless: bool, max_iterations: int, max_cost_usd: float
) -> orchestrator_loop.RunResult:
    configure_logging()
    # In a hosted, multi-user deployment the request layer sets DOSSIER_PRINCIPAL
    # to the authenticated caller so cached content is partitioned per user and
    # never leaks across identities. Unset on the single-user CLI -> shared cache.
    cache = CacheStore(scope=os.environ.get("DOSSIER_PRINCIPAL"))
    log_path = DEFAULT_RUNS_DIR / f"{_run_id_for(question)}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    emitter = EventEmitter(_run_id_for(question), log_path)

    gate = DomainGate.load(DEFAULT_DOMAINS_PATH, allow_seed=_SEED_ALLOW_DOMAINS)
    dictionary = DictionaryClient()

    tool_provider = await _build_tool_provider(cache)

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
                tool_provider=tool_provider,
                max_iterations=max_iterations,
                max_cost_usd=max_cost_usd,
            )
        finally:
            if tool_provider is not None:
                await tool_provider.aclose()
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


def _eval_sample(question: str, result: orchestrator_loop.RunResult) -> dict[str, object]:
    """A `{question, answer, contexts}` eval sample from a finished run — the input
    `devtools eval` judges, and the unit of an A/B test of a scoring change."""
    return {"question": question, "answer": result.answer, "contexts": result.contexts}


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
    capture_eval: Path = typer.Option(
        None,
        "--capture-eval",
        help="Append this run as a {question, answer, contexts} eval sample to a JSONL "
        "(for `devtools eval` / A-B testing scoring changes).",
    ),
) -> None:
    """Ask Dossier a question once and print the cited answer."""
    result = asyncio.run(
        _ask(question, headless=headless, max_iterations=max_iterations, max_cost_usd=max_cost_usd)
    )
    _print_result(result)
    if capture_eval is not None:
        with capture_eval.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_eval_sample(question, result)) + "\n")
        console.print(f"[dim]captured eval sample → {capture_eval}[/dim]")


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
    # In a hosted, multi-user deployment the request layer sets DOSSIER_PRINCIPAL
    # to the authenticated caller so cached content is partitioned per user and
    # never leaks across identities. Unset on the single-user CLI -> shared cache.
    cache = CacheStore(scope=os.environ.get("DOSSIER_PRINCIPAL"))
    # One gate for the whole session, so a domain approved for the first
    # question isn't re-prompted for the next.
    gate = DomainGate.load(DEFAULT_DOMAINS_PATH, allow_seed=_SEED_ALLOW_DOMAINS)
    dictionary = DictionaryClient()
    tool_provider = await _build_tool_provider(cache)
    async with BrowserSession(DEFAULT_USER_DATA_DIR, headless=headless) as session:
        registry = _build_registry(cache, session)
        setup_emitter = EventEmitter("interactive-session")
        fetcher = WebFetcher(session, cache, setup_emitter)
        console.print("[bold]Dossier interactive mode[/bold] - Ctrl+D or 'exit' to quit.\n")
        try:
            while True:
                try:
                    question = typer.prompt("dossier>")
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
                    tool_provider=tool_provider,
                    max_iterations=max_iterations,
                    max_cost_usd=max_cost_usd,
                )
                _print_result(result)
                console.print()
                emitter.close()
        finally:
            if tool_provider is not None:
                await tool_provider.aclose()
            await fetcher.aclose()
            await dictionary.aclose()
            cache.close()


@app.command()
def desktop() -> None:
    """Open Dossier as a native desktop window (Chainlit UI in an OS webview).
    Requires the 'desktop' extra: `pip install ".\\[desktop]"`."""
    from dossier.ui.desktop import run

    run()


@app.command()
def ui() -> None:
    """Launch the desktop chat UI (Chainlit). Requires the 'ui' extra:
    `pip install ".\\[ui]"`. Opens a local chat window over the same engine."""
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("chainlit") is None:
        # typer.echo (not rich console) so the '[ui]' extra name prints literally.
        typer.echo("Chainlit is not installed. Run: pip install '.[ui]'")
        raise typer.Exit(1)
    app_path = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    raise typer.Exit(subprocess.run(["chainlit", "run", str(app_path)], check=False).returncode)


if __name__ == "__main__":
    app()
