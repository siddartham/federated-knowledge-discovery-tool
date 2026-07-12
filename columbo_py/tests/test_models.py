from __future__ import annotations

import pytest
from pydantic import ValidationError

from columbo_py.engine.orchestrator.models import Confidence


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
