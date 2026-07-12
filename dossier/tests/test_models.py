from __future__ import annotations

import pytest
from pydantic import ValidationError

from dossier.engine.orchestrator.models import Confidence, ScoreResult, scored_composite


def test_relevance_gate_fixes_the_off_topic_soft_spot() -> None:
    # off-topic but authoritative (low relevance, high everything else) vs.
    # on-topic (high relevance, modest rest).
    off = dict(direct_relevance=1, answer_potential=8, context_value=8, source_quality=13)
    on = dict(direct_relevance=13, answer_potential=8, context_value=5, source_quality=3)
    # under the plain mean, the off-topic item OUT-ranks the on-topic one — the soft spot
    assert scored_composite(**off, gated=False) > scored_composite(**on, gated=False)
    # under the gate, relevance is a prerequisite: the on-topic item wins
    assert scored_composite(**on, gated=True) > scored_composite(**off, gated=True)


def test_relevance_gate_zeros_an_irrelevant_item() -> None:
    # relevance 0 -> composite 0 no matter how strong the other three are
    assert scored_composite(0, 13, 13, 13, gated=True) == 0.0
    # ungated, the same item scores well (the behaviour the gate corrects)
    assert scored_composite(0, 13, 13, 13, gated=False) == pytest.approx(39 / 52)


def test_score_result_composite_uses_the_configured_gate() -> None:
    # default config ships relevance_gated = true, so an irrelevant result gates to 0
    r = ScoreResult(source="x", direct_relevance=0, answer_potential=13,
                    context_value=13, source_quality=13)
    assert r.composite == 0.0


def test_confidence_composite_is_mean_of_four_dimensions() -> None:
    c = Confidence(
        explicit_evidence=13,
        implicit_evidence=13,
        evidence_consistency=13,
        answer_specificity=13,
    )
    assert c.composite == 1.0

    c2 = Confidence(
        explicit_evidence=13,
        implicit_evidence=0,
        evidence_consistency=0,
        answer_specificity=0,
    )
    assert c2.composite == pytest.approx(13 / 52)


def test_confidence_requires_all_four_dimensions() -> None:
    # evidence_consistency is now required - a 3-dimension payload must fail
    # rather than silently scoring on a partial set.
    with pytest.raises(ValidationError):
        Confidence.model_validate(
            {"explicit_evidence": 5, "implicit_evidence": 5, "answer_specificity": 5}
        )


def test_confidence_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        Confidence(
            explicit_evidence=47,
            implicit_evidence=5,
            evidence_consistency=5,
            answer_specificity=5,
        )
