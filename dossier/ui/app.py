"""Chainlit chat front-end for Dossier.

Launch with `dossier ui` (or `chainlit run dossier/ui/app.py`); requires the
`ui` extra (`pip install ".[ui]"`). This module is the only place Chainlit is
imported - all engine glue lives in `ui.events` / `ui.session`, so the engine
itself is untouched.

Each user message runs one `orchestrator.run()`; the engine's structured events
are streamed to the chat as collapsible Steps (the "thinking"), and the final
cited Markdown answer is posted as the assistant message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import chainlit as cl

from dossier.infra.events.emitter import configure_logging
from dossier.ui.events import UIEvent, UIEventEmitter
from dossier.ui.session import WarmSession

_DONE = "__ui_done__"


def _describe(event: str, fields: dict[str, Any]) -> str | None:
    """Human label for the events worth surfacing as a Step; None to hide."""
    if event == "plan_complete":
        return f"🧭 planned (confidence {fields.get('confidence', 0):.2f})"
    if event == "search_complete":
        return f"🔎 searched {fields.get('source', '')} — {fields.get('result_count', 0)} results"
    if event == "scrape_complete":
        return f"🌐 fetched {fields.get('url', '')}"
    if event == "lookup_complete":
        return f"📇 looked up {fields.get('source', '')}"
    if event == "acronym_resolved":
        return f"🔤 {fields.get('acronym', '')} = {fields.get('definition', '')}"
    if event == "score_complete":
        return "⚖️ scored evidence"
    if event == "synthesize_complete":
        return "✍️ synthesized answer"
    return None


@cl.on_chat_start
async def on_chat_start() -> None:
    configure_logging()
    session = WarmSession(headless=False)
    await session.start()
    cl.user_session.set("dossier", session)
    await cl.Message(
        content="Ask me anything across your team's Slack, Confluence, GitHub, Jira, or Drive."
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    session: WarmSession = cl.user_session.get("dossier")
    queue: asyncio.Queue[UIEvent] = asyncio.Queue(maxsize=2000)
    emitter = UIEventEmitter(f"ui-{message.id}", queue)

    async def drain() -> None:
        while True:
            event, fields = await queue.get()
            if event == _DONE:
                return
            label = _describe(event, fields)
            if label is not None:
                async with cl.Step(name=label):
                    pass

    drainer = asyncio.create_task(drain())
    try:
        result = await session.ask(message.content, emitter)
    finally:
        queue.put_nowait((_DONE, {}))
        await drainer

    footer = (
        f"\n\n---\n*{result.iterations} iteration(s) · "
        f"confidence {result.final_confidence:.2f} · "
        f"est. ${result.cost_usd:.4f} · {result.terminated_reason}*"
    )
    await cl.Message(content=result.answer + footer).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    session = cl.user_session.get("dossier")
    if session is not None:
        await session.close()
