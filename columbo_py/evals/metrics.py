"""Pydantic shapes for the LLM-as-judge verdicts, plus the normalized
per-sample score bundle.

Each judge scores on the same 0-13 Fibonacci-like scale the rest of Columbo
uses (see `engine.orchestrator.models`), so raw judge output validates the same
way; `SampleScores` normalizes everything to [0, 1] for reporting.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from columbo_py.engine.orchestrator.models import MAX_SCORE


class FaithfulnessVerdict(BaseModel):
    """How well the answer's claims are grounded in the contexts."""

    score: int = Field(ge=0, le=MAX_SCORE)
    unsupported_claims: list[str] = Field(default_factory=list)
    reason: str = ""


class RelevancyVerdict(BaseModel):
    """How directly the answer addresses the question."""

    score: int = Field(ge=0, le=MAX_SCORE)
    reason: str = ""


class ContextVerdict(BaseModel):
    id: str
    relevant: bool


class ContextPrecisionVerdict(BaseModel):
    """Per-context relevant/noise judgment over the selected evidence."""

    verdicts: list[ContextVerdict] = Field(default_factory=list)
    reason: str = ""

    def precision(self, context_count: int) -> float:
        """Fraction of the retrieved contexts judged relevant. Normalizes
        against `context_count` (the number actually shown to the judge) rather
        than len(verdicts), so a judge that drops a verdict can't inflate the
        score. 0.0 when there were no contexts to judge."""
        if context_count <= 0:
            return 0.0
        relevant = sum(1 for v in self.verdicts if v.relevant)
        return relevant / context_count


@dataclass(frozen=True, slots=True)
class SampleScores:
    """The three metric scores for one evaluated sample, each in [0, 1]."""

    faithfulness: float
    answer_relevancy: float
    context_precision: float
    faithfulness_reason: str = ""
    relevancy_reason: str = ""
    precision_reason: str = ""
    unsupported_claims: tuple[str, ...] = ()

    @property
    def overall(self) -> float:
        """Unweighted mean of the three metrics - a single headline number."""
        return (self.faithfulness + self.answer_relevancy + self.context_precision) / 3


def normalize(points: int) -> float:
    """Map a 0-13 judge score to [0, 1]."""
    return points / MAX_SCORE
