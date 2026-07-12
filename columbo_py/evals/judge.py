"""Native LLM-as-judge evaluation - no external eval framework.

Reuses Columbo's own infrastructure: the `LLMClient` (so judge calls hit the
same on-disk response cache and are therefore deterministic and free on re-run),
the Jinja prompt renderer, and the same 0-13 scoring scale as the runtime
pipeline. Three reference-free metrics run concurrently per sample:

- faithfulness: are the answer's claims grounded in the contexts? (hallucination)
- answer_relevancy: does the answer address the question that was asked?
- context_precision: is the retrieved+selected evidence actually relevant?

Kept deliberately offline (driven by `devtools eval`, never the `ask` hot path)
so it never perturbs the runtime loop's determinism or cost.
"""

from __future__ import annotations

import asyncio

from columbo_py.config import SETTINGS
from columbo_py.engine.llm.client import LLMClient
from columbo_py.engine.llm.parsing import extract_json
from columbo_py.engine.prompts.render import render_prompt
from columbo_py.evals.dataset import EvalSample
from columbo_py.evals.metrics import (
    ContextPrecisionVerdict,
    FaithfulnessVerdict,
    RelevancyVerdict,
    SampleScores,
    normalize,
)


async def _judge(llm: LLMClient, template: str, request_type: str, model: str, **ctx: object) -> object:
    system_prompt, user_prompt = render_prompt(template, **ctx)
    response = await llm.generate(
        system_prompt=system_prompt, prompt=user_prompt, model=model, request_type=request_type
    )
    return extract_json(response.text)


async def _faithfulness(llm: LLMClient, sample: EvalSample, model: str) -> FaithfulnessVerdict:
    data = await _judge(
        llm, "eval_faithfulness.j2", "eval_faithfulness", model,
        question=sample.question, answer=sample.answer, contexts=sample.contexts,
    )
    return FaithfulnessVerdict.model_validate(data)


async def _relevancy(llm: LLMClient, sample: EvalSample, model: str) -> RelevancyVerdict:
    data = await _judge(
        llm, "eval_answer_relevancy.j2", "eval_answer_relevancy", model,
        question=sample.question, answer=sample.answer,
    )
    return RelevancyVerdict.model_validate(data)


async def _context_precision(
    llm: LLMClient, sample: EvalSample, model: str
) -> ContextPrecisionVerdict:
    if not sample.contexts:
        # Nothing retrieved - no precision call to make. Empty verdict -> 0.0.
        return ContextPrecisionVerdict(reason="no contexts to judge")
    data = await _judge(
        llm, "eval_context_precision.j2", "eval_context_precision", model,
        question=sample.question, contexts=sample.contexts,
    )
    return ContextPrecisionVerdict.model_validate(data)


async def evaluate_sample(
    llm: LLMClient, sample: EvalSample, *, model: str | None = None
) -> SampleScores:
    """Score one sample on all three metrics, running the judges concurrently.
    `model` defaults to the configured judge model ([models].judge)."""
    judge_model = model or SETTINGS.models.judge
    faith, rel, prec = await asyncio.gather(
        _faithfulness(llm, sample, judge_model),
        _relevancy(llm, sample, judge_model),
        _context_precision(llm, sample, judge_model),
    )
    return SampleScores(
        faithfulness=normalize(faith.score),
        answer_relevancy=normalize(rel.score),
        context_precision=prec.precision(len(sample.contexts)),
        faithfulness_reason=faith.reason,
        relevancy_reason=rel.reason,
        precision_reason=prec.reason,
        unsupported_claims=tuple(faith.unsupported_claims),
    )
