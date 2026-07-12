"""Offline LLM-as-judge evaluation harness for Columbo (see `devtools eval`)."""

from __future__ import annotations

from columbo_py.evals.dataset import Context, EvalSample, load_samples, sample_from_dict
from columbo_py.evals.judge import evaluate_sample
from columbo_py.evals.metrics import SampleScores

__all__ = [
    "Context",
    "EvalSample",
    "SampleScores",
    "evaluate_sample",
    "load_samples",
    "sample_from_dict",
]
