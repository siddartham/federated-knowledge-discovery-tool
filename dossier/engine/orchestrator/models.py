"""Pydantic models for every JSON shape the LLM emits or consumes.

Scores are integers on a 0-13 Fibonacci-like scale (0, 1, 2, 3, 5, 8, 13) so that
confidence buckets stay coarse enough for an LLM to grade consistently. Pydantic's
range validation replaces Go's silent zero-value-on-bad-unmarshal behavior: a
response with a score of 47 or "high" fails loudly here instead of quietly
becoming 0.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from dossier.config import SETTINGS

SCORE_SCALE = (0, 1, 2, 3, 5, 8, 13)
MAX_SCORE = 13


def scored_composite(
    direct_relevance: int,
    answer_potential: int,
    context_value: int,
    source_quality: int,
    *,
    gated: bool,
) -> float:
    """Evidence composite in [0, 1] for a search result or restitched scrape span.

    `gated=True` treats `direct_relevance` as a **prerequisite**, not a term: it
    multiplies the mean of the other three, so an off-topic item scores near zero no
    matter how authoritative. `gated=False` is the plain equal-weighted mean of all
    four. Relevance is the one dimension a high score on the others should not redeem;
    the other three are fungible (background vs. answer vs. quality), so they average.
    See docs/scoring-and-confidence.md ("Mean or multiplication?").
    """
    if gated:
        rest = (answer_potential + context_value + source_quality) / (3 * MAX_SCORE)
        return (direct_relevance / MAX_SCORE) * rest
    return (direct_relevance + answer_potential + context_value + source_quality) / (4 * MAX_SCORE)


class Confidence(BaseModel):
    """Emitted by the orchestrator (plan) call each iteration. Four dimensions,
    each scored 0-13 against an anchored rubric in the orchestrate prompt:
    explicit (direct answers), implicit (contextual signals), consistency
    (agreement across sources), and specificity (how precise an answer is
    possible)."""

    explicit_evidence: int = Field(ge=0, le=MAX_SCORE)
    implicit_evidence: int = Field(ge=0, le=MAX_SCORE)
    evidence_consistency: int = Field(ge=0, le=MAX_SCORE)
    answer_specificity: int = Field(ge=0, le=MAX_SCORE)

    @property
    def composite(self) -> float:
        """Mean of the four dimensions normalized to [0, 1]. Compared against
        the 0.8 cutoff."""
        return (
            self.explicit_evidence
            + self.implicit_evidence
            + self.evidence_consistency
            + self.answer_specificity
        ) / (4 * MAX_SCORE)


class SearchAction(BaseModel):
    source: str
    query: str


class ToolCall(BaseModel):
    """A structured call to an MCP tool: a tool name + arguments matching that
    tool's input schema (as advertised to the planner)."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Actions(BaseModel):
    searches: list[SearchAction] = Field(default_factory=list)
    scrapes: list[str] = Field(default_factory=list)
    lookups: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class OrchestrationResponse(BaseModel):
    """LLM #1 (plan) output: what to search/scrape/look up next, and how confident
    the orchestrator already is given evidence gathered so far."""

    thinking: str
    confidence: Confidence
    actions: Actions


class ScoreResult(BaseModel):
    """LLM #2 (score, cheap model e.g. Haiku) output: one per raw search result."""

    source: str
    direct_relevance: int = Field(ge=0, le=MAX_SCORE)
    answer_potential: int = Field(ge=0, le=MAX_SCORE)
    context_value: int = Field(ge=0, le=MAX_SCORE)
    source_quality: int = Field(ge=0, le=MAX_SCORE)

    @property
    def composite(self) -> float:
        return scored_composite(
            self.direct_relevance,
            self.answer_potential,
            self.context_value,
            self.source_quality,
            gated=SETTINGS.evidence.relevance_gated,
        )


class RestitchResult(BaseModel):
    """LLM #3 (restitch, per scraped page) output: which chunk of a scraped page
    is worth keeping, and how good it is."""

    chunk_start: int = Field(ge=0)
    chunk_end: int = Field(ge=0)
    direct_relevance: int = Field(ge=0, le=MAX_SCORE)
    answer_potential: int = Field(ge=0, le=MAX_SCORE)
    context_value: int = Field(ge=0, le=MAX_SCORE)
    source_quality: int = Field(ge=0, le=MAX_SCORE)

    @property
    def composite(self) -> float:
        return scored_composite(
            self.direct_relevance,
            self.answer_potential,
            self.context_value,
            self.source_quality,
            gated=SETTINGS.evidence.relevance_gated,
        )


class Citation(BaseModel):
    source: str
    id: str
    permalink: str | None = None


class SynthesisResponse(BaseModel):
    """LLM #4 (synthesize, e.g. Opus) output: the final free-text, cited answer."""

    answer: str
    citations: list[Citation] = Field(default_factory=list)


class ActionStats(BaseModel):
    """Running counters surfaced on the event log after each fan-out phase."""

    searches: int = 0
    scrapes: int = 0
    lookups: int = 0
    results: int = 0
