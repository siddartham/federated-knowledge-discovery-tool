"""Playwright persistent-context session manager.

`launch_persistent_context` is the direct Playwright equivalent of chromedp's
`UserDataDir` option: cookies, localStorage, and SSO state persist across
process restarts in `user_data_dir`, so a user who's already logged into
Slack/Confluence/Jira in that profile stays logged in for Columbo without the
app ever touching a password or a service-account token - satisfying the
"multi-source auth w/o provisioning tokens" constraint from the spec.

Only one BrowserSession should be live per user_data_dir at a time (Chromium
locks the profile directory); concurrent fetches share one session and get
their own tab via `new_tab()`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

DEFAULT_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)


class BrowserSession:
    def __init__(self, user_data_dir: Path, headless: bool = False) -> None:
        self.user_data_dir = user_data_dir
        self.headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=list(DEFAULT_LAUNCH_ARGS),
        )

    async def stop(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserSession.start() must be awaited before use")
        return self._context

    async def new_tab(self) -> Page:
        """Caller is responsible for `await page.close()` - use as an async
        context manager via `columbo_py.infra.browser.session.tab(session)`
        when you don't need the page to outlive one operation."""
        return await self.context.new_page()

    async def evaluate_in_page(self, url: str, js: str) -> Any:
        """Navigate a fresh tab to `url`, run `js`, close the tab. For
        one-shot extraction (e.g. reading an auth token off `window`) where
        you don't need the page to stick around."""
        page = await self.new_tab()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return await page.evaluate(js)
        finally:
            await page.close()

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()
