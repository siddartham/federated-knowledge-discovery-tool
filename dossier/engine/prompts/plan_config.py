"""Tunable numeric knobs for the plan prompt (orchestrate.j2), kept in Python
instead of hardcoded in the template so they live in one place and can be
overridden at runtime (see `run(..., plan_config=...)`).

The score scale is loaded from `models.MAX_SCORE`, so the prompt's rubric and
the Pydantic score validation can never drift apart - change the scale once and
both move together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dossier.config import SETTINGS
from dossier.engine.orchestrator.models import MAX_SCORE

# Anchor points shown in each confidence rubric, as fractions of the score
# scale. Chosen to reproduce the calibrated 0/4/7/10/13 gradations at
# MAX_SCORE=13, and to scale sensibly if the scale is ever changed. Configured
# in config/defaults.toml ([plan].anchor_fractions).
_ANCHOR_FRACTIONS = SETTINGS.plan.anchor_fractions


@dataclass(frozen=True, slots=True)
class PlanConfig:
    """Numeric parameters injected into the plan prompt at render time.
    Field defaults come from config/defaults.toml ([plan]); override per-run by
    constructing a PlanConfig and passing it to `run(..., plan_config=...)`."""

    max_score: int = MAX_SCORE
    max_searches: int = SETTINGS.plan.max_searches
    max_scrapes: int = SETTINGS.plan.max_scrapes
    max_lookups: int = SETTINGS.plan.max_lookups
    min_source_types: int = SETTINGS.plan.min_source_types  # spread across >= this many sources

    @property
    def score_anchors(self) -> list[int]:
        """The five rubric anchor integers across the 0..max_score scale."""
        return [round(f * self.max_score) for f in _ANCHOR_FRACTIONS]


DEFAULT_PLAN_CONFIG = PlanConfig()


def plan_prompt_context(config: PlanConfig = DEFAULT_PLAN_CONFIG) -> dict[str, Any]:
    """The numeric variables orchestrate.j2 references, as a render context dict.
    Kept next to the template's other render args in `_plan`, and reused by the
    prompt tests so the template's required variables have a single source."""
    return {
        "max_score": config.max_score,
        "score_anchors": config.score_anchors,
        "max_searches": config.max_searches,
        "max_scrapes": config.max_scrapes,
        "max_lookups": config.max_lookups,
        "min_source_types": config.min_source_types,
    }
