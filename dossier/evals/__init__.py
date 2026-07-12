"""Offline LLM-as-judge evaluation harness for Dossier (see `devtools eval`)."""

from __future__ import annotations

from dossier.evals.dataset import Context, EvalSample, load_samples, sample_from_dict
from dossier.evals.judge import evaluate_sample
from dossier.evals.metrics import SampleScores

__all__ = [
    "Context",
    "EvalSample",
    "SampleScores",
    "evaluate_sample",
    "load_samples",
    "sample_from_dict",
]
