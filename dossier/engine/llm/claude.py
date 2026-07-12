"""Anthropic SDK client wired for two layers of caching:

1. Anthropic's own prompt cache (`cache_control: ephemeral` on the system
   block) - saves latency/cost on a *miss* to our disk cache, e.g. repeated
   calls that share a system prompt but vary the user prompt (score/restitch
   calls share a system prompt across every result in a batch).
2. An on-disk response cache keyed on (model, system_prompt, user_prompt) -
   buys byte-for-byte determinism across runs: identical question + identical
   state hits this cache and never calls the API at all.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from dossier.config import SETTINGS
from dossier.engine.llm.client import LLMResponse
from dossier.infra.cache.store import CacheStore, prompt_cache_key
from dossier.infra.events.emitter import EventEmitter

MAX_TOKENS = SETTINGS.models.max_tokens


class ClaudeClient:
    def __init__(
        self, cache: CacheStore, emitter: EventEmitter, api_key: str | None = None
    ) -> None:
        # AsyncAnthropic reads ANTHROPIC_API_KEY from the environment if api_key is None.
        self._client = AsyncAnthropic(api_key=api_key)
        self._cache = cache
        self._emitter = emitter

    async def generate(
        self, *, system_prompt: str, prompt: str, model: str, request_type: str = ""
    ) -> LLMResponse:
        cache_key = prompt_cache_key(system_prompt, prompt, model)
        cached_text = self._cache.response_cache.get(cache_key)
        if cached_text is not None:
            self._emitter.emit(
                "cache_hit", cache="response", request_type=request_type, model=model
            )
            return LLMResponse(
                text=cached_text,
                model=model,
                request_type=request_type,
                input_tokens=0,
                output_tokens=0,
                cached=True,
            )

        self._emitter.emit(
            "llm_request", request_type=request_type, model=model, prompt_chars=len(prompt)
        )

        response = await self._client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": prompt}],
        )

        text = "".join(block.text for block in response.content if block.type == "text")
        self._cache.response_cache.set(cache_key, text)
        self._emitter.emit(
            "llm_response",
            request_type=request_type,
            model=model,
            cached=False,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return LLMResponse(
            text=text,
            model=model,
            request_type=request_type,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached=False,
        )
