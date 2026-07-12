"""A scripted LLMClient so orchestrator/actions/state code can be tested end
to end without a network call or an ANTHROPIC_API_KEY. Feed it the exact JSON
strings you want returned, in call order.
"""

from __future__ import annotations

from collections import deque

from columbo_py.engine.llm.client import LLMResponse


class MockLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses: deque[str] = deque(responses)
        self.calls: list[dict[str, str]] = []

    async def generate(
        self, *, system_prompt: str, prompt: str, model: str, request_type: str = ""
    ) -> LLMResponse:
        self.calls.append(
            {
                "request_type": request_type,
                "model": model,
                "system_prompt": system_prompt,
                "prompt": prompt,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"MockLLMClient ran out of scripted responses (call #{len(self.calls)}, "
                f"request_type={request_type!r})"
            )
        text = self._responses.popleft()
        return LLMResponse(
            text=text,
            model=model,
            request_type=request_type,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            cached=False,
        )
