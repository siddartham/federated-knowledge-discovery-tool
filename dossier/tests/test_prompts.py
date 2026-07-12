"""Tests for the assembled plan (orchestrate.j2) prompt: both the rendered
template in isolation and what _plan actually sends to the LLM through run()."""

from __future__ import annotations

import json

from dossier.engine.llm.mock import MockLLMClient
from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.prompts.plan_config import PlanConfig, plan_prompt_context
from dossier.engine.prompts.render import render_prompt
from dossier.engine.search.registry import Registry
from dossier.infra.cache.store import CacheStore
from dossier.infra.events.emitter import EventEmitter
from dossier.sources.confluence import ConfluenceSource
from dossier.sources.github import GitHubCodeSource

_CONFIDENCE_KEYS = (
    "explicit_evidence",
    "implicit_evidence",
    "evidence_consistency",
    "answer_specificity",
)


def _registry(cache: CacheStore) -> Registry:
    registry = Registry(cache)
    registry.register(GitHubCodeSource(token="t"))
    registry.register(ConfluenceSource(base_url="https://x/wiki", email="a@b.com", api_token="t"))
    return registry


def test_orchestrate_system_block_has_guides_dimensions_and_rules(cache: CacheStore) -> None:
    system_prompt, _ = render_prompt(
        "orchestrate.j2",
        sources_block=_registry(cache).sources_prompt_block(),
        question="q",
        iteration=1,
        max_iterations=6,
        confidence_cutoff=0.8,
        evidence_summary="",
        **plan_prompt_context(),
    )

    # Source guides, keyed by the exact source key the planner must emit.
    assert '### source key: "github_code"' in system_prompt
    assert '### source key: "confluence"' in system_prompt
    assert "GITHUB (code search)" in system_prompt
    assert "CQL" in system_prompt

    # All four confidence dimensions are present and rubric-anchored.
    for key in _CONFIDENCE_KEYS:
        assert key in system_prompt, f"missing confidence dimension: {key}"
    assert "no direct answer found in any evidence" in system_prompt  # a rubric anchor

    # Planner guidance carried over from the reference template.
    assert "RULES:" in system_prompt
    assert "Do NOT repeat" in system_prompt
    assert "ACTION GUIDELINES" in system_prompt

    # Coverage rule reliably pulls Slack into the mix.
    assert "COVERAGE:" in system_prompt
    assert 'include at least one "slack" search' in system_prompt

    # Lookups are for ids/handles, explicitly NOT acronyms (handled elsewhere).
    assert "never spend a lookup on one" in system_prompt


def test_orchestrate_numeric_values_come_from_plan_config(cache: CacheStore) -> None:
    # Every numeric in the prompt is injected at render time, not hardcoded:
    # a non-default PlanConfig must flow through verbatim, and the score scale
    # is loaded from models.MAX_SCORE (so prompt and validation can't drift).
    from dossier.engine.orchestrator.models import MAX_SCORE

    config = PlanConfig(max_searches=7, max_scrapes=5, max_lookups=3, min_source_types=3)
    system_prompt, user_prompt = render_prompt(
        "orchestrate.j2",
        sources_block=_registry(cache).sources_prompt_block(),
        question="q",
        iteration=1,
        max_iterations=6,
        confidence_cutoff=0.8,
        evidence_summary="",
        **plan_prompt_context(config),
    )
    assert "up to ~7 search queries" in system_prompt
    assert "up to ~5 scrapes and ~3 lookups" in system_prompt
    assert "at least 3 DIFFERENT sources" in system_prompt
    # Score scale + top rubric anchor both come from MAX_SCORE.
    assert f"score each 0-{MAX_SCORE}" in system_prompt
    assert f"<0-{MAX_SCORE}>" in system_prompt
    assert str(config.score_anchors[-1]) == str(MAX_SCORE)


def test_orchestrate_user_block_carries_question_and_digest(cache: CacheStore) -> None:
    digest = "- searched 'confluence' for 'auth': 3 results\nTop 1 evidence items by score so far:"
    _, user_prompt = render_prompt(
        "orchestrate.j2",
        sources_block=_registry(cache).sources_prompt_block(),
        question="How does token refresh work?",
        iteration=2,
        max_iterations=6,
        confidence_cutoff=0.8,
        evidence_summary=digest,
        **plan_prompt_context(),
    )
    assert "How does token refresh work?" in user_prompt
    assert "2 of 6" in user_prompt
    assert "0.8" in user_prompt
    assert digest in user_prompt


def test_orchestrate_user_block_first_iteration_has_no_evidence(cache: CacheStore) -> None:
    _, user_prompt = render_prompt(
        "orchestrate.j2",
        sources_block=_registry(cache).sources_prompt_block(),
        question="q",
        iteration=1,
        max_iterations=6,
        confidence_cutoff=0.8,
        evidence_summary="",
        **plan_prompt_context(),
    )
    assert "nothing yet" in user_prompt


class _NullFetcher:
    async def fetch(self, url: str) -> str:
        return ""


def _plan_json(confidence: int) -> str:
    return json.dumps(
        {
            "thinking": "t",
            "confidence": {k: confidence for k in _CONFIDENCE_KEYS},
            "actions": {"searches": [], "scrapes": [], "lookups": []},
        }
    )


_SYNTH = json.dumps({"answer": "answer", "citations": []})


async def test_plan_call_assembles_full_prompt_through_run(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # End-to-end: confirm _plan actually feeds the source guides (system block)
    # and the question (user block) into the LLM call, not just that the
    # template *can* render them. High confidence terminates after one plan
    # call, before any source is queried.
    registry = _registry(cache)
    llm = MockLLMClient([_plan_json(13), _SYNTH])

    await orchestrator_loop.run(
        "How does token refresh work?", registry, _NullFetcher(), llm, emitter
    )

    plan_call = next(c for c in llm.calls if c["request_type"] == "plan")
    assert '### source key: "github_code"' in plan_call["system_prompt"]
    assert "evidence_consistency" in plan_call["system_prompt"]
    assert "RULES:" in plan_call["system_prompt"]
    assert "How does token refresh work?" in plan_call["prompt"]
