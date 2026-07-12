"""Engine-side contract for MCP-style tool calling, kept free of any concrete
source import so the orchestrator never depends on `sources.mcp` at runtime.

- `ToolProvider`: the structural interface the loop/executor needs (list tools,
  call a tool). `sources.mcp.MCPToolProvider` satisfies it.
- `select_tools`: narrow a large catalog to the K tools most relevant to the
  question, so the plan prompt never carries hundreds of schemas. Lexical and
  deterministic (score desc, then name) so the plan prompt - and its response
  cache - stay stable; swap in an embedding/LLM ranker without changing callers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from dossier.config import SETTINGS
from dossier.engine.llm.parsing import extract_json
from dossier.engine.prompts.render import render_prompt

if TYPE_CHECKING:  # type-only; no runtime import of the source layer
    from dossier.engine.llm.client import LLMClient
    from dossier.engine.orchestrator.cost import CostTracker
    from dossier.infra.events.emitter import EventEmitter
    from dossier.sources.mcp.client import ToolContent, ToolSpec

TOOL_TOP_K = 12  # tools shown to the planner per iteration
MAX_TOOL_CALLS = 8  # tool calls executed per iteration
ROUTE_MODEL = SETTINGS.models.score  # semantic routing reuses the cheap scoring tier (Haiku)


@runtime_checkable
class ToolProvider(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]: ...
    async def aclose(self) -> None: ...


def _tokenize(text: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", text.lower()) if len(word) > 2}


def select_tools(question: str, catalog: list[ToolSpec], k: int = TOOL_TOP_K) -> list[ToolSpec]:
    """The k tools whose name+description overlap the question most. Returns all
    of them when the catalog already fits within k. Lexical and dependency-free;
    also the fallback for `select_tools_semantic` when the router call fails."""
    if len(catalog) <= k:
        return list(catalog)
    q = _tokenize(question)
    return sorted(
        catalog,
        key=lambda t: (-len(q & _tokenize(f"{t.name} {t.description}")), t.name),
    )[:k]


async def select_tools_semantic(
    question: str,
    catalog: list[ToolSpec],
    llm: LLMClient,
    *,
    k: int = TOOL_TOP_K,
    cost: CostTracker | None = None,
    emitter: EventEmitter | None = None,
) -> list[ToolSpec]:
    """Ask the model which k tools best fit the question, matching it against
    each tool's DESCRIPTION rather than shared tokens - so a tool survives even
    when it shares no vocabulary with the question ("intermittent" vs "flaky").

    Degrades, never disappears: an empty pick, a malformed reply, or a model
    error falls back to the lexical `select_tools`. No LLM call is made when the
    catalog already fits within k. The prompt keys off the question alone, so
    the response caches per-question and re-runs hit the cache.
    """
    if len(catalog) <= k:
        return list(catalog)
    by_name = {t.name: t for t in catalog}
    system_prompt, user_prompt = render_prompt(
        "select_tools.j2",
        question=question,
        tools=[{"name": t.name, "description": t.description} for t in catalog],
        k=k,
    )
    try:
        response = await llm.generate(
            system_prompt=system_prompt,
            prompt=user_prompt,
            model=ROUTE_MODEL,
            request_type="route",
        )
        if cost is not None:
            cost.record(ROUTE_MODEL, response.input_tokens, response.output_tokens)
        names = extract_json(response.text)
        picks = (
            [by_name[n] for n in names if isinstance(n, str) and n in by_name]
            if isinstance(names, list)
            else []
        )
    except Exception as exc:  # malformed JSON, model/transport error - degrade to lexical
        if emitter is not None:
            emitter.emit("tool_route_fallback", error=str(exc)[:200], catalog_size=len(catalog))
        return select_tools(question, catalog, k)
    if not picks:
        if emitter is not None:
            emitter.emit("tool_route_fallback", error="empty_selection", catalog_size=len(catalog))
        return select_tools(question, catalog, k)
    if emitter is not None:
        emitter.emit("tool_route", catalog_size=len(catalog), selected=len(picks[:k]))
    return picks[:k]
