"""Eval sample model + a JSONL loader.

A sample is the reference-free triple the judges need - a question, the answer
Dossier produced, and the evidence contexts it was grounded in - plus an
optional reference answer for future label-based metrics. Samples can be
hand-authored (see `sample_dataset.jsonl`) or captured from a real run's
selected evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Context:
    id: str
    content: str


@dataclass(frozen=True, slots=True)
class EvalSample:
    question: str
    answer: str
    contexts: list[Context] = field(default_factory=list)
    reference: str | None = None  # optional ground-truth answer (unused by the reference-free judges)


def _coerce_context(raw: Any, index: int) -> Context:
    """Accept either a bare string context or a {"id", "content"} object; a
    missing id falls back to a positional `ctx{n}` so the judge can still
    reference it."""
    if isinstance(raw, str):
        return Context(id=f"ctx{index}", content=raw)
    if isinstance(raw, dict):
        content = str(raw.get("content", "")).strip()
        cid = str(raw.get("id") or "").strip() or f"ctx{index}"
        # Compose an id from source/id when present (matches evidence citations).
        if raw.get("source") and raw.get("id"):
            cid = f"{raw['source']}:{raw['id']}"
        return Context(id=cid, content=content)
    raise ValueError(f"context #{index} must be a string or object, got {type(raw).__name__}")


def sample_from_dict(data: dict[str, Any]) -> EvalSample:
    if "question" not in data or "answer" not in data:
        raise ValueError("eval sample requires 'question' and 'answer' keys")
    contexts = [_coerce_context(c, i) for i, c in enumerate(data.get("contexts", []))]
    reference = data.get("reference")
    return EvalSample(
        question=str(data["question"]),
        answer=str(data["answer"]),
        contexts=contexts,
        reference=str(reference) if reference is not None else None,
    )


def load_samples(path: Path) -> list[EvalSample]:
    """Load a JSONL file, one eval sample per non-blank line."""
    samples: list[EvalSample] = []
    for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(sample_from_dict(json.loads(line)))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return samples
