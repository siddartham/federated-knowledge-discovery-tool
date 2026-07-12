"""Tests for the offline run-log analysis (devtools analyze)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from columbo_py.evals.analyze import (
    SCORE_DIMS,
    build_report,
    calibration,
    citation_analysis,
    confidence_rows,
    correlation,
    flags,
    gate_comparison,
    score_rows,
    spearman,
)


def test_spearman_monotonic_and_inverse() -> None:
    assert spearman([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)  # perfectly monotone
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)  # perfectly inverse
    assert math.isnan(spearman([5, 5, 5], [1, 2, 3]))  # no variance -> undefined


def _score_event(run_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [{"id": r["id"], **{d: r[d] for d in SCORE_DIMS},
               "composite": sum(r[d] for d in SCORE_DIMS) / (4 * 13)} for r in rows]
    return {"event": "score_complete", "ok": True, "run_id": run_id, "scores": scores}


def test_score_rows_and_correlation_flags_a_correlated_pair() -> None:
    # direct_relevance and answer_potential move together; source_quality is independent.
    rows: list[dict[str, Any]] = [
        {"id": "a", "direct_relevance": 1, "answer_potential": 1, "context_value": 5, "source_quality": 8},
        {"id": "b", "direct_relevance": 5, "answer_potential": 5, "context_value": 2, "source_quality": 3},
        {"id": "c", "direct_relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 1},
        {"id": "d", "direct_relevance": 13, "answer_potential": 13, "context_value": 1, "source_quality": 13},
    ]
    events: list[dict[str, Any]] = [_score_event("r1", rows)]
    sr = score_rows(events)
    assert len(sr) == 4
    corr = correlation(sr, SCORE_DIMS)
    assert corr["direct_relevance|answer_potential"] == pytest.approx(1.0)  # identical -> flagged
    assert any("direct_relevance <-> answer_potential" in f for f in flags(sr, SCORE_DIMS))


def test_citation_analysis_ranks_and_below_threshold() -> None:
    events: list[dict[str, Any]] = [
        _score_event("r1", [
            {"id": "x", "direct_relevance": 13, "answer_potential": 13, "context_value": 13, "source_quality": 13},
            {"id": "y", "direct_relevance": 1, "answer_potential": 1, "context_value": 1, "source_quality": 1},
        ]),
        {"event": "synthesize_complete", "run_id": "r1",
         "cited": [{"source": "s", "id": "x"}, {"source": "s", "id": "y"}]},
    ]
    ca = citation_analysis(events)
    assert ca["citations"] == 2
    assert ca["matched_to_a_score"] == 2
    # y is the low-scored, low-ranked citation -> counted below the admit bar
    assert ca["cited_below_admit_threshold"] == 1


def test_restitch_dims_join_the_analysis() -> None:
    # restitch_complete rows contribute to the correlation set and the citation map.
    events: list[dict[str, Any]] = [
        _score_event("r1", [
            {"id": "s1", "direct_relevance": 8, "answer_potential": 8, "context_value": 3, "source_quality": 5},
        ]),
        {"event": "restitch_complete", "run_id": "r1", "url": "http://x/p",
         "direct_relevance": 13, "answer_potential": 13, "context_value": 8, "source_quality": 8,
         "composite": round((13 + 13 + 8 + 8) / 52, 4)},
        {"event": "synthesize_complete", "run_id": "r1", "cited": [{"source": "web", "id": "http://x/p"}]},
    ]
    rows = score_rows(events)
    assert len(rows) == 2  # the search score AND the restitch span
    ca = citation_analysis(events)
    assert ca["matched_to_a_score"] == 1  # the cited scrape url resolved via restitch_complete
    assert ca["cited_below_admit_threshold"] == 0  # it scored high


def test_gate_comparison_detects_arm_and_reranks() -> None:
    # logs scored under the MEAN, citing the on-topic item that the mean under-ranks.
    good = {"id": "good", "direct_relevance": 13, "answer_potential": 8, "context_value": 5,
            "source_quality": 3, "composite": round((13 + 8 + 5 + 3) / 52, 4)}
    off = {"id": "off", "direct_relevance": 1, "answer_potential": 8, "context_value": 8,
           "source_quality": 13, "composite": round((1 + 8 + 8 + 13) / 52, 4)}
    events: list[dict[str, Any]] = [
        {"event": "score_complete", "ok": True, "run_id": "r1", "scores": [good, off]},
        {"event": "synthesize_complete", "run_id": "r1", "cited": [{"source": "s", "id": "good"}]},
    ]
    gc = gate_comparison(events)
    assert gc["logs_produced_under"] == "mean"  # logged composites match the mean formula
    assert gc["cited_evaluated"] == 1
    # under the mean, 'off' (0.577) outranks 'good' (0.558) -> good is rank 2;
    # under the gate, 'good' wins -> rank 1. The gate promotes the cited item.
    assert gc["median_rank_cited_mean"] == 2
    assert gc["median_rank_cited_gated"] == 1
    assert gc["cited_promoted_by_gate"] == 1


def test_build_report_includes_gate_comparison_only_when_requested() -> None:
    events: list[dict[str, Any]] = [
        _score_event("r1", [{"id": "x", "direct_relevance": 8, "answer_potential": 5,
                             "context_value": 3, "source_quality": 8}]),
        {"event": "synthesize_complete", "run_id": "r1", "cited": [{"source": "s", "id": "x"}]},
    ]
    assert "gate_comparison" not in build_report(events)
    assert build_report(events, compare_gate=True)["gate_comparison"]["cited_evaluated"] == 1


def test_calibration_bins_join_on_question() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_start", "run_id": "r1", "question": "Q1"},
        {"event": "run_end", "run_id": "r1", "final_confidence": 0.9},
        {"event": "run_start", "run_id": "r2", "question": "Q2"},
        {"event": "run_end", "run_id": "r2", "final_confidence": 0.5},
    ]
    bins = calibration(events, {"Q1": 0.8, "Q2": 0.4})
    high = next(b for b in bins if b["confidence"].startswith("[0.8"))
    assert high["n"] == 1 and high["mean_faithfulness"] == pytest.approx(0.8)


def test_build_report_end_to_end() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_start", "run_id": "r1", "question": "Q"},
        {"event": "plan_complete", "run_id": "r1", "confidence": 0.5,
         "explicit_evidence": 8, "implicit_evidence": 5, "evidence_consistency": 3, "answer_specificity": 5},
        _score_event("r1", [
            {"id": "x", "direct_relevance": 8, "answer_potential": 5, "context_value": 3, "source_quality": 8},
        ]),
        {"event": "synthesize_complete", "run_id": "r1", "cited": [{"source": "s", "id": "x"}]},
        {"event": "run_end", "run_id": "r1", "final_confidence": 0.5, "terminated_reason": "no_actions_proposed"},
    ]
    report = build_report(events)
    assert report["runs"] == 1
    assert report["scored_items"] == 1
    assert report["plan_iterations"] == 1
    assert report["citations"]["citations"] == 1
    assert report["termination"] == {"no_actions_proposed": 1}
    assert report["calibration"] is None  # no evals index supplied
    assert len(confidence_rows(events)) == 1
