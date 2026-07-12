"""LLMClient Protocol - the one seam between the orchestrator and whichever
model actually answers plan/score/restitch/synthesize calls. Swappable for a
mock in tests, or for a different provider without touching orchestrator code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    request_type: str
    input_tokens: int
    output_tokens: int
    cached: bool


@runtime_checkable
class LLMClient(Protocol):
    async def generate(
        self, *, system_prompt: str, prompt: str, model: str, request_type: str = ""
    ) -> LLMResponse: ...
