"""Tests for the offline LLM-as-judge evaluation harness, driven entirely by
MockLLMClient - no network, no ANTHROPIC_API_KEY."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from columbo_py.engine.llm.mock import MockLLMClient
from columbo_py.evals import evaluate_sample, load_samples, sample_from_dict
from columbo_py.evals.dataset import Context, EvalSample
from columbo_py.evals.metrics import ContextPrecisionVerdict, ContextVerdict


def _mock(faith: str, rel: str, prec: str) -> MockLLMClient:
    # evaluate_sample gathers in this order: faithfulness, relevancy, precision.
    return MockLLMClient([faith, rel, prec])


async def test_perfect_sample_scores_one() -> None:
    sample = EvalSample(
        question="q",
        answer="a",
        contexts=[Context(id="s:1", content="c1"), Context(id="s:2", content="c2")],
    )
    llm = _mock(
        json.dumps({"score": 13, "unsupported_claims": [], "reason": "grounded"}),
        json.dumps({"score": 13, "reason": "on point"}),
        json.dumps({"verdicts": [{"id": "s:1", "relevant": True}, {"id": "s:2", "relevant": True}]}),
    )
    scores = await evaluate_sample(llm, sample)
    assert scores.faithfulness == 1.0
    assert scores.answer_relevancy == 1.0
    assert scores.context_precision == 1.0
    assert scores.overall == 1.0


async def test_hallucinated_and_noisy_sample_scores_low() -> None:
    sample = EvalSample(
        question="q",
        answer="a",
        contexts=[Context(id="s:1", content="c1"), Context(id="s:2", content="c2")],
    )
    llm = _mock(
        json.dumps({"score": 0, "unsupported_claims": ["made up X"], "reason": "fabricated"}),
        json.dumps({"score": 3, "reason": "off topic"}),
        # only one of two contexts relevant -> precision 0.5
        json.dumps({"verdicts": [{"id": "s:1", "relevant": True}, {"id": "s:2", "relevant": False}]}),
    )
    scores = await evaluate_sample(llm, sample)
    assert scores.faithfulness == 0.0
    assert scores.answer_relevancy == pytest.approx(3 / 13)
    assert scores.context_precision == 0.5
    assert scores.unsupported_claims == ("made up X",)


async def test_no_contexts_skips_precision_call() -> None:
    """With no contexts, the precision judge must not be called (no scripted
    third response) and precision is 0.0."""
    sample = EvalSample(question="q", answer="a", contexts=[])
    llm = MockLLMClient(
        [
            json.dumps({"score": 8, "unsupported_claims": [], "reason": "ok"}),
            json.dumps({"score": 13, "reason": "answers it"}),
        ]
    )
    scores = await evaluate_sample(llm, sample)
    assert scores.context_precision == 0.0
    assert scores.faithfulness == pytest.approx(8 / 13)
    # Exactly two judge calls were made (faithfulness + relevancy), not three.
    assert len(llm.calls) == 2
    assert {c["request_type"] for c in llm.calls} == {"eval_faithfulness", "eval_answer_relevancy"}


def test_precision_normalizes_against_context_count() -> None:
    # Judge dropped a verdict for the second context; precision must divide by
    # the true context count (2), not the number of verdicts (1).
    verdict = ContextPrecisionVerdict(verdicts=[ContextVerdict(id="a", relevant=True)])
    assert verdict.precision(2) == 0.5
    assert verdict.precision(0) == 0.0


def test_load_samples_and_context_coercion(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(
        json.dumps({"question": "q1", "answer": "a1", "contexts": ["bare string ctx"]})
        + "\n\n"  # blank line tolerated
        + json.dumps(
            {"question": "q2", "answer": "a2", "contexts": [{"source": "slack", "id": "9", "content": "x"}]}
        )
        + "\n",
        encoding="utf-8",
    )
    samples = load_samples(path)
    assert len(samples) == 2
    assert samples[0].contexts[0].id == "ctx0"  # bare string gets a positional id
    assert samples[1].contexts[0].id == "slack:9"  # source:id composed


def test_sample_from_dict_requires_question_and_answer() -> None:
    with pytest.raises(ValueError, match="requires 'question' and 'answer'"):
        sample_from_dict({"question": "q"})


def test_shipped_sample_dataset_is_loadable() -> None:
    path = Path(__file__).parent.parent / "evals" / "sample_dataset.jsonl"
    samples = load_samples(path)
    assert len(samples) >= 2
    assert all(s.contexts for s in samples)
