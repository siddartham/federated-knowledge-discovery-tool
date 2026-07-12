"""Playwright CDP event tap: forwards the page events that matter for
observability (crashes, failed navigations, console errors) onto the shared
event log, so a browser dying mid-scrape shows up as `browser_crashed` +
`browser_recovery` in the run log instead of a silent hang.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dossier.infra.events.emitter import EventEmitter

if TYPE_CHECKING:
    from playwright.async_api import Page, Response


def attach_page_listener(page: Page, emitter: EventEmitter) -> None:
    page.on("crash", lambda p: emitter.emit("browser_crashed", url=p.url))
    page.on(
        "pageerror",
        lambda exc: emitter.emit("browser_console_error", url=page.url, error=str(exc)),
    )

    def _on_response(response: Response) -> None:
        if response.status >= 400:
            emitter.emit(
                "browser_navigated", url=response.url, status=response.status, ok=False
            )

    page.on("response", _on_response)
