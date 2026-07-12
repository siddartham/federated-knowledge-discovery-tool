"""End-to-end tests for the input filter (oversized question rejected before any
LLM call) and the output guard (a secret surfaced in the answer is redacted)."""

from __future__ import annotations

import json
from pathlib import Path

from columbo_py.config import SETTINGS
from columbo_py.engine.llm.mock import MockLLMClient
from columbo_py.engine.orchestrator import loop as orchestrator_loop
from columbo_py.engine.search.registry import Registry
from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.events.emitter import EventEmitter

_CONFIDENCE_KEYS = (
    "explicit_evidence",
    "implicit_evidence",
    "evidence_consistency",
    "answer_specificity",
)


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


async def test_oversized_question_rejected_without_llm_calls(tmp_path: Path) -> None:
    llm = MockLLMClient([])  # any LLM call would raise (no scripted responses)
    question = "x" * (SETTINGS.guardrails.max_question_chars + 1)
    result = await orchestrator_loop.run(
        question, Registry(CacheStore(tmp_path)), _NullFetcher(), llm, EventEmitter("t")
    )
    assert result.terminated_reason == "input_rejected"
    assert result.iterations == 0
    assert llm.calls == []  # rejected before planning


async def test_secret_in_answer_is_redacted(tmp_path: Path) -> None:
    # High confidence -> terminates after one plan call, then synthesizes.
    synth = json.dumps(
        {"answer": "The deploy key is AKIAABCDEFGHIJKLMNOP - use it.", "citations": []}
    )
    llm = MockLLMClient([_plan_json(13), synth])
    result = await orchestrator_loop.run(
        "how do I deploy?", Registry(CacheStore(tmp_path)), _NullFetcher(), llm, EventEmitter("t")
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in result.answer
    assert "«redacted" in result.answer
