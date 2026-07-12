from __future__ import annotations

from columbo_py.engine.orchestrator.models import RestitchResult, ScoreResult
from columbo_py.engine.orchestrator.state import SearchBatch, ScrapeResult, State, select_evidence
from columbo_py.engine.search.result import Result


def _result(id_: str, source: str = "github", content: str = "x") -> Result:
    return Result(source=source, id=id_, title=f"title-{id_}", content=content, permalink=None)


def test_select_evidence_respects_min_guarantee_over_threshold() -> None:
    state = State(question="q")
    state.batches.append(
        SearchBatch(source="github", query="q", results=[_result("1"), _result("2")])
    )
    # Both score below threshold, but min_guarantee=2 should still include both.
    state.scored_results["1"] = ScoreResult(
        source="1", relevance=0, answer_potential=0, context_value=0, source_quality=0
    )
    state.scored_results["2"] = ScoreResult(
        source="2", relevance=1, answer_potential=0, context_value=0, source_quality=0
    )

    evidence = select_evidence(state, score_threshold=0.9, min_guarantee=2, char_budget=10_000)
    assert {e.id for e in evidence} == {"1", "2"}


def test_select_evidence_excludes_below_threshold_past_min_guarantee() -> None:
    state = State(question="q")
    state.batches.append(
        SearchBatch(source="github", query="q", results=[_result("1"), _result("2"), _result("3")])
    )
    state.scored_results["1"] = ScoreResult(
        source="1", relevance=13, answer_potential=13, context_value=13, source_quality=13
    )
    state.scored_results["2"] = ScoreResult(
        source="2", relevance=0, answer_potential=0, context_value=0, source_quality=0
    )
    state.scored_results["3"] = ScoreResult(
        source="3", relevance=0, answer_potential=0, context_value=0, source_quality=0
    )

    evidence = select_evidence(state, score_threshold=0.5, min_guarantee=1, char_budget=10_000)
    assert [e.id for e in evidence] == ["1"]


def test_select_evidence_is_deterministic_regardless_of_insertion_order() -> None:
    """Two States built with results appended in a different order must
    produce the identical evidence ordering - this is what makes the LLM
    response cache actually hit across runs where asyncio.gather completed
    tasks in a different order."""

    def build(order: list[str]) -> State:
        state = State(question="q")
        results = [_result(i) for i in order]
        state.batches.append(SearchBatch(source="github", query="q", results=results))
        for i in order:
            state.scored_results[i] = ScoreResult(
                source=i, relevance=8, answer_potential=8, context_value=8, source_quality=8
            )
        return state

    state_a = build(["1", "2", "3"])
    state_b = build(["3", "1", "2"])

    evidence_a = select_evidence(state_a, min_guarantee=3)
    evidence_b = select_evidence(state_b, min_guarantee=3)
    assert [e.id for e in evidence_a] == [e.id for e in evidence_b]


def test_select_evidence_includes_scraped_pages_with_nonempty_span() -> None:
    state = State(question="q")
    state.scrapes.append(ScrapeResult(url="http://x", content="line1\nline2\nline3"))
    state.scored_scrapes["http://x"] = RestitchResult(
        chunk_start=1,
        chunk_end=2,
        direct_relevance=13,
        answer_potential=13,
        context_value=13,
        source_quality=13,
    )

    evidence = select_evidence(state, min_guarantee=1)
    assert len(evidence) == 1
    assert evidence[0].content == "line1\nline2"
    assert evidence[0].origin == "scrape"


def test_evidence_summary_surfaces_top_scores() -> None:
    # The planner digest must carry the quality signal (composite scores),
    # not just result counts, so confidence isn't decoupled from evidence
    # quality.
    state = State(question="q")
    state.batches.append(
        SearchBatch(source="github", query="tokens", results=[_result("1"), _result("2")])
    )
    state.scored_results["1"] = ScoreResult(
        source="1", relevance=13, answer_potential=13, context_value=13, source_quality=13
    )
    state.scored_results["2"] = ScoreResult(
        source="2", relevance=1, answer_potential=0, context_value=0, source_quality=0
    )

    summary = state.evidence_summary()
    assert "searched 'github' for 'tokens': 2 results" in summary
    assert "Top 2 evidence items by score" in summary
    # Highest composite (1.00) listed before the low one, with the id.
    assert summary.index("github:1") < summary.index("github:2")
    assert "[1.00] github:1" in summary


def test_select_evidence_skips_zero_width_restitch_span() -> None:
    state = State(question="q")
    state.scrapes.append(ScrapeResult(url="http://x", content="line1\nline2"))
    state.scored_scrapes["http://x"] = RestitchResult(
        chunk_start=1,
        chunk_end=1,
        direct_relevance=0,
        answer_potential=0,
        context_value=0,
        source_quality=0,
    )

    evidence = select_evidence(state, min_guarantee=1)
    assert evidence == []
