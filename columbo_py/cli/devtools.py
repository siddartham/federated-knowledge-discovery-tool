"""Dev tools for Columbo:

- `smoke`   no-credentials pipeline sanity check (in-memory source + scripted
            LLM responses) - proves plan/execute/score/synthesize wiring is
            intact without an API key, network access, or a browser.
- `plan`    the real initial plan call only (one Sonnet call), no browser and
            no source credentials - needs just ANTHROPIC_API_KEY. Prints the
            planner's proposed actions + confidence without executing them.
- `demo`    the real `ask` pipeline with a canned default question, for
            showing someone Columbo without them typing anything.
- `bench`   aggregates metrics (iterations, confidence, cost, cache hit rate)
            across every run log in a directory.
- `compare` diffs the final-run metrics of two run logs - a quick regression
            check when tuning prompts or thresholds against the same question.
- `analyze` validates the scoring/confidence design against a directory of run
            logs: dimension-correlation, scorer-vs-citations, and (with --evals)
            confidence calibration. See docs/scoring-and-confidence.md.

`bench` and `compare` both just parse the JSON-lines run log every real run
already writes (see `infra/events/emitter.py`) - no separate instrumentation.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

import typer

from columbo_py.config import SETTINGS
from columbo_py.engine.llm.mock import MockLLMClient
from columbo_py.engine.orchestrator import loop as orchestrator_loop
from columbo_py.engine.search.registry import Registry
from columbo_py.engine.search.result import Options, Result
from columbo_py.engine.search.source import Capabilities
from columbo_py.infra.cache.store import (
    CACHE_NAMESPACES,
    DEFAULT_CACHE_ROOT,
    CacheStore,
)
from columbo_py.infra.events.emitter import EventEmitter, configure_logging

app = typer.Typer()

DEFAULT_RUNS_DIR = SETTINGS.paths.runs_dir
DEMO_QUESTION = SETTINGS.devtools.demo_question


class _DemoSource:
    """An in-memory fake source with a couple of canned documents, used by
    `smoke` so the whole pipeline can be exercised with zero credentials and
    zero network calls."""

    _DOCS = [
        Result(
            source="demo",
            id="1",
            title="Columbo overview",
            content=(
                "Columbo is a CLI agent that answers questions across Slack, "
                "Confluence, GitHub, Drive, and Jira by scoring evidence and "
                "synthesizing a cited answer with Claude."
            ),
            permalink="demo://1",
        ),
        Result(
            source="demo",
            id="2",
            title="Deployment model",
            content=(
                "Columbo runs entirely on the user's machine via a persistent "
                "Playwright browser profile - no server component, and no "
                "service-account tokens to provision."
            ),
            permalink="demo://2",
        ),
    ]

    def name(self) -> str:
        return "demo"

    def kind(self) -> str:
        return "An in-memory demo source with a couple of canned documents about Columbo itself."

    async def initialize(self) -> None:
        pass

    async def search(self, query: str, opts: Options) -> list[Result]:
        return list(self._DOCS)

    def capabilities(self) -> Capabilities:
        return Capabilities()


class _DemoFetcher:
    async def fetch(self, url: str) -> str:
        return "(demo fetcher - no real scraping happens in smoke mode)"


@app.command()
def smoke() -> None:
    """Run the full plan/execute/score/synthesize loop against an in-memory
    demo source and scripted LLM responses. Exits non-zero if the pipeline
    raises - useful as a fast CI check that doesn't need any credentials."""
    configure_logging()

    plan = json.dumps(
        {
            "thinking": "the demo source should have what I need",
            "confidence": {"explicit_evidence": 3, "implicit_evidence": 3, "evidence_consistency": 3, "answer_specificity": 2},
            "actions": {
                "searches": [{"source": "demo", "query": "what is columbo"}],
                "scrapes": [],
                "lookups": [],
            },
        }
    )
    score = json.dumps(
        [
            {"source": "1", "direct_relevance": 13, "answer_potential": 13, "context_value": 8, "source_quality": 13},
            {"source": "2", "direct_relevance": 8, "answer_potential": 5, "context_value": 8, "source_quality": 8},
        ]
    )
    plan2 = json.dumps(
        {
            "thinking": "enough evidence now",
            "confidence": {"explicit_evidence": 13, "implicit_evidence": 13, "evidence_consistency": 13, "answer_specificity": 13},
            "actions": {"searches": [], "scrapes": [], "lookups": []},
        }
    )
    synth = json.dumps(
        {
            "answer": (
                "Columbo is a CLI agent for cross-source Q&A [demo:1], "
                "running locally via a persistent browser profile [demo:2]."
            ),
            "citations": [
                {"source": "demo", "id": "1", "permalink": "demo://1"},
                {"source": "demo", "id": "2", "permalink": "demo://2"},
            ],
        }
    )

    async def _run() -> orchestrator_loop.RunResult:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cache = CacheStore(Path(tmp))
            emitter = EventEmitter("smoke-test")
            registry = Registry(cache)
            registry.register(_DemoSource())
            llm = MockLLMClient([plan, score, plan2, synth])
            try:
                return await orchestrator_loop.run(
                    DEMO_QUESTION, registry, _DemoFetcher(), llm, emitter
                )
            finally:
                cache.close()

    result = asyncio.run(_run())
    typer.echo(f"OK: {result.iterations} iteration(s), confidence {result.final_confidence:.2f}")
    typer.echo(result.answer)


@app.command("clear-cache")
def clear_cache(
    namespace: str = typer.Argument(
        "all",
        help=f"Which cache to clear: all|{'|'.join(CACHE_NAMESPACES)}",
    ),
) -> None:
    """Clear Columbo's on-disk caches. By default clears all four namespaces
    (response/search/lookup/scrape); pass a namespace to clear just one, e.g.
    `clear-cache search` to force fresh source queries while keeping cached
    LLM responses."""
    target = None if namespace == "all" else namespace
    cache = CacheStore()
    try:
        cleared = cache.clear(target)
    except ValueError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(code=1)
    finally:
        cache.close()

    for name, count in cleared.items():
        typer.echo(f"  {name}: cleared {count} entries")
    typer.echo(f"Total {sum(cleared.values())} entries cleared from {DEFAULT_CACHE_ROOT}")


@app.command()
def plan(
    question: str = typer.Argument(..., help="Question to run the initial plan call for"),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Also run the proposed searches/lookups for one iteration "
        "(fan-out + scoring), so you can see the source calls and their logs.",
    ),
) -> None:
    """Exercise the initial plan call - the first Sonnet plan/assess step -
    against real Claude, with no browser and no source credentials.

    The plan prompt reads each source's GUIDANCE.md from disk, and sources only
    initialize lazily on execute, so the single thing you need set is
    ANTHROPIC_API_KEY. Prints the planner's proposed actions + four-dimension
    confidence WITHOUT executing them.

    Pass --execute to also run the proposed actions for one iteration: the
    search fan-out fires (emitting search_complete events you can watch), any
    results are scored, and a per-action outcome log is printed. Sources still
    need their own env vars to return real results - unconfigured ones run but
    log an error rather than crashing, which is itself useful to see."""
    from columbo_py.engine.llm.claude import ClaudeClient
    from columbo_py.engine.orchestrator.actions import enrich_acronyms, execute_actions
    from columbo_py.engine.orchestrator.cost import CostTracker
    from columbo_py.engine.orchestrator.state import State
    from columbo_py.sources.confluence import ConfluenceSource
    from columbo_py.sources.dictionary import DictionaryClient
    from columbo_py.sources.drive import DriveSource
    from columbo_py.sources.github import GitHubCodeSource, GitHubIssuesSource
    from columbo_py.sources.jira import JiraSource
    from columbo_py.sources.slack import SlackSource

    configure_logging()

    async def _run() -> None:
        cache = CacheStore()
        emitter = EventEmitter("devtools-plan")
        registry = Registry(cache)
        # Constructed credential-free; sources initialize lazily on first
        # search(), so no env vars (and no browser) are needed for the plan
        # call itself - only --execute reaches them.
        for src in (
            GitHubCodeSource(),
            GitHubIssuesSource(),
            SlackSource(),
            ConfluenceSource(),
            JiraSource(),
            DriveSource(),
        ):
            registry.register(src)
        llm = ClaudeClient(cache, emitter)
        dictionary = DictionaryClient()
        cost = CostTracker(orchestrator_loop.DEFAULT_MAX_COST_USD)
        state = State(question=question)
        try:
            # Same pre-plan step run() does: resolve the question's acronyms
            # against the dictionary so they appear in the plan prompt - and so
            # you can see the acronym_resolved events fire here.
            await enrich_acronyms(state, dictionary, emitter)
            response = await orchestrator_loop._plan(
                state,
                registry,
                llm,
                emitter,
                cost,
                orchestrator_loop.CONFIDENCE_CUTOFF,
                iteration=1,
                max_iterations=orchestrator_loop.DEFAULT_MAX_ITERATIONS,
            )
            c = response.confidence
            actions = response.actions
            if state.dictionary:
                typer.echo("\nAcronyms resolved (via dictionary, fed into the plan prompt):")
                for term, definition in state.dictionary.items():
                    typer.echo(f"  {term}: {definition}")
            typer.echo(f"\nThinking: {response.thinking}\n")
            typer.echo(
                f"Confidence: explicit={c.explicit_evidence} implicit={c.implicit_evidence} "
                f"consistency={c.evidence_consistency} specificity={c.answer_specificity} "
                f"-> composite {c.composite:.2f}"
            )
            typer.echo(
                f"\nProposed actions ({len(actions.searches)} searches, "
                f"{len(actions.scrapes)} scrapes, {len(actions.lookups)} lookups):"
            )
            for s in actions.searches:
                typer.echo(f"  search  {s.source}: {s.query!r}")
            for url in actions.scrapes:
                typer.echo(f"  scrape  {url}")
            for term in actions.lookups:
                typer.echo(f"  lookup  {term!r}")

            if execute:
                typer.echo("\nExecuting proposed actions (watch the *_complete events above)...")
                await execute_actions(
                    state, actions, registry, _DemoFetcher(), llm, emitter,
                    cost=cost, dictionary=dictionary,
                )
                typer.echo("\nExecution log (per action):")
                for line in state.actions_taken or ["(no actions ran)"]:
                    typer.echo(f"  {line}")

            typer.echo(f"\nest. cost ${cost.spent_usd:.4f}")
        finally:
            await dictionary.aclose()
            cache.close()

    asyncio.run(_run())


@app.command("eval")
def evaluate(
    samples_path: Path = typer.Argument(
        Path(__file__).parent.parent / "evals" / "sample_dataset.jsonl",
        help="JSONL of {question, answer, contexts[, reference]} samples to score.",
    ),
    model: str = typer.Option(
        "", help="Judge model override; defaults to config [models].judge (Sonnet)."
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit non-zero if mean faithfulness < [guardrails].min_faithfulness (CI gate).",
    ),
    out: Path = typer.Option(
        None,
        "--out",
        help="Write per-sample {question, faithfulness, ...} JSONL for `devtools analyze --evals`.",
    ),
) -> None:
    """Offline LLM-as-judge evaluation: score answers on faithfulness (are the
    claims grounded in the contexts?), answer-relevancy (does it address the
    question?), and context-precision (is the retrieved evidence relevant?).

    Reads a JSONL sample file and runs each sample through the judge, reusing
    Columbo's own Claude client + response cache (so re-runs are deterministic
    and free). Needs only ANTHROPIC_API_KEY - no sources, no browser. This is a
    separate offline gate; it never runs inside `ask`."""
    from columbo_py.engine.llm.claude import ClaudeClient
    from columbo_py.evals import evaluate_sample, load_samples

    configure_logging()
    samples = load_samples(samples_path)
    if not samples:
        typer.echo(f"no samples found in {samples_path}")
        raise typer.Exit(1)

    async def _run() -> None:
        cache = CacheStore()
        emitter = EventEmitter("devtools-eval")
        llm = ClaudeClient(cache, emitter)
        try:
            scored = []
            for i, sample in enumerate(samples, 1):
                s = await evaluate_sample(llm, sample, model=(model or None))
                scored.append(s)
                typer.echo(f"\n[{i}] {sample.question}")
                typer.echo(
                    f"    faithfulness={s.faithfulness:.2f}  "
                    f"answer_relevancy={s.answer_relevancy:.2f}  "
                    f"context_precision={s.context_precision:.2f}  "
                    f"overall={s.overall:.2f}"
                )
                if s.unsupported_claims:
                    typer.echo(f"    unsupported: {'; '.join(s.unsupported_claims)}")

            n = len(scored)
            typer.echo(f"\n=== Aggregate (mean over {n} sample(s)) ===")
            typer.echo(f"  faithfulness      {sum(x.faithfulness for x in scored) / n:.3f}")
            typer.echo(f"  answer_relevancy  {sum(x.answer_relevancy for x in scored) / n:.3f}")
            typer.echo(f"  context_precision {sum(x.context_precision for x in scored) / n:.3f}")
            typer.echo(f"  overall           {sum(x.overall for x in scored) / n:.3f}")

            if out is not None:
                with out.open("w", encoding="utf-8") as fh:
                    for sample, s in zip(samples, scored):
                        fh.write(json.dumps({
                            "question": sample.question,
                            "faithfulness": s.faithfulness,
                            "answer_relevancy": s.answer_relevancy,
                            "context_precision": s.context_precision,
                            "overall": s.overall,
                        }) + "\n")
                typer.echo(f"\nwrote {n} score(s) to {out}  (feed to `devtools analyze --evals`)")

            if check:
                mean_faithfulness = sum(x.faithfulness for x in scored) / n
                floor = SETTINGS.guardrails.min_faithfulness
                if mean_faithfulness < floor:
                    typer.echo(
                        f"\nFAIL: mean faithfulness {mean_faithfulness:.3f} < "
                        f"min_faithfulness {floor:.3f}"
                    )
                    raise typer.Exit(1)
                typer.echo(f"\nPASS: mean faithfulness {mean_faithfulness:.3f} >= {floor:.3f}")
        finally:
            cache.close()

    asyncio.run(_run())


@app.command()
def demo(question: str = typer.Argument(DEMO_QUESTION)) -> None:
    """Run the real `ask` pipeline (real sources, real Claude calls) against
    a canned default question - for showing Columbo to someone without them
    needing to think of a question first."""
    from columbo_py.cli.main import _ask
    from columbo_py.cli.main import _print_result as _print

    result = asyncio.run(
        _ask(
            question,
            headless=False,
            max_iterations=orchestrator_loop.DEFAULT_MAX_ITERATIONS,
            max_cost_usd=orchestrator_loop.DEFAULT_MAX_COST_USD,
        )
    )
    _print(result)


@app.command()
def bench(run_log_dir: Path = typer.Argument(DEFAULT_RUNS_DIR)) -> None:
    """Aggregate metrics across every run log (*.jsonl) in `run_log_dir`."""
    events = _load_events_dir(run_log_dir)
    if not events:
        typer.echo(f"no run logs found in {run_log_dir}")
        raise typer.Exit(1)

    run_ends = [e for e in events if e.get("event") == "run_end"]
    cache_hits = sum(1 for e in events if e.get("event") == "cache_hit")
    cache_misses = sum(
        1 for e in events if e.get("event") in ("llm_request", "search_complete", "scrape_complete")
    )

    if run_ends:
        iterations = [e["iterations"] for e in run_ends]
        confidences = [e["final_confidence"] for e in run_ends]
        costs = [e["cost_usd"] for e in run_ends]
        typer.echo(f"runs: {len(run_ends)}")
        typer.echo(f"iterations: mean={statistics.mean(iterations):.2f} max={max(iterations)}")
        typer.echo(f"final_confidence: mean={statistics.mean(confidences):.2f} min={min(confidences):.2f}")
        typer.echo(f"cost_usd: mean=${statistics.mean(costs):.4f} total=${sum(costs):.4f}")

    total_cacheable = cache_hits + cache_misses
    rate = (cache_hits / total_cacheable) if total_cacheable else 0.0
    typer.echo(f"cache hit rate: {cache_hits}/{total_cacheable} ({rate:.1%})")


@app.command()
def compare(run_a: Path, run_b: Path) -> None:
    """Diff the final `run_end` metrics of two run logs - a quick regression
    check when tuning prompts/thresholds against the same question."""
    a = _final_run_end(_load_events_file(run_a))
    b = _final_run_end(_load_events_file(run_b))
    if a is None or b is None:
        typer.echo("one or both run logs have no run_end event")
        raise typer.Exit(1)

    for key in ("iterations", "final_confidence", "cost_usd", "terminated_reason"):
        typer.echo(f"{key}: {a.get(key)!r} -> {b.get(key)!r}")


@app.command()
def analyze(
    runs_dir: Path = typer.Argument(DEFAULT_RUNS_DIR, help="Directory of run .jsonl logs"),
    evals: Path = typer.Option(
        None, help="Optional JSONL of {question, faithfulness} to calibrate confidence"
    ),
    compare_gate: bool = typer.Option(
        False,
        "--compare-gate",
        help="Recompute mean vs. relevance-gated rankings from the logged dims and diff them.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw report as JSON"),
) -> None:
    """Validate the scoring/confidence design against logged runs: dimension
    independence (correlation), scorer-vs-citations, confidence calibration (with
    --evals), and a mean-vs-gated ranking diff (with --compare-gate). Read-only;
    just parses the run logs. See docs/scoring-and-confidence.md."""
    from columbo_py.evals.analyze import (
        build_report,
        format_report,
        load_evals_index,
        load_events,
    )

    events = load_events(runs_dir)
    if not events:
        typer.echo(f"no run logs found in {runs_dir}")
        raise typer.Exit(1)
    index = load_evals_index(evals) if evals else None
    report = build_report(events, index, compare_gate=compare_gate)
    typer.echo(json.dumps(report, indent=2, default=str) if as_json else format_report(report))


def _load_events_dir(run_log_dir: Path) -> list[dict[str, Any]]:
    if not run_log_dir.exists():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(run_log_dir.glob("*.jsonl")):
        events.extend(_load_events_file(path))
    return events


def _load_events_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _final_run_end(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    run_ends = [e for e in events if e.get("event") == "run_end"]
    return run_ends[-1] if run_ends else None
