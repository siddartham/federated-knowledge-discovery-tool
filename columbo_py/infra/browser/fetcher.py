"""WebFetcher: renders a URL through the shared browser session and returns
its readable text content, backed by the scrape disk cache (keyed on the
normalized URL). Recovers from a crashed browser session by restarting it
once before falling back to a plain httpx GET - browser stability over long
sessions is one of the explicit failure modes this system has to survive.
"""

from __future__ import annotations

import re
from typing import Protocol, cast, runtime_checkable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from columbo_py.config import SETTINGS
from columbo_py.infra.browser.session import BrowserSession
from columbo_py.infra.cache.store import SCRAPE_CACHE_TTL_S, CacheStore, normalize_url
from columbo_py.infra.events.emitter import EventEmitter
from columbo_py.infra.events.listener import attach_page_listener

# From config/defaults.toml ([http]).
MAX_CONTENT_CHARS = SETTINGS.http.max_content_chars  # guard against one huge page
BROWSER_NAV_TIMEOUT_MS = SETTINGS.http.browser_nav_timeout_ms

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n\s*")


def _html_to_text(html: str) -> str:
    """Crude, dependency-free HTML-to-text. The browser path returns
    `innerText`; the httpx fallback would otherwise return raw markup, so
    the scoring/restitch stages downstream would see tags instead of prose.
    This keeps the fallback's content shape roughly consistent with the
    primary path without pulling in a parser dependency."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return _BLANKLINES_RE.sub("\n\n", text).strip()


@runtime_checkable
class Fetcher(Protocol):
    """What the orchestrator actually needs from a fetcher - narrower than
    the concrete WebFetcher class, so tests can pass a trivial fake instead
    of a real browser-backed instance."""

    async def fetch(self, url: str) -> str: ...


class WebFetcher:
    def __init__(self, session: BrowserSession, cache: CacheStore, emitter: EventEmitter) -> None:
        self._session = session
        self._cache = cache
        self._emitter = emitter
        self._http = httpx.AsyncClient(
            follow_redirects=True, timeout=SETTINGS.http.default_timeout_s
        )

    async def fetch(self, url: str) -> str:
        cache_key = normalize_url(url)
        cached = self._cache.scrape_cache.get(cache_key)
        if cached is not None:
            self._emitter.emit("cache_hit", cache="scrape", url=url)
            return cast(str, cached)

        try:
            content = await self._fetch_via_browser(url)
        except Exception as exc:
            self._emitter.emit("browser_crashed", url=url, error=str(exc))
            content = await self._recover_and_retry(url)

        content = content[:MAX_CONTENT_CHARS]
        self._cache.scrape_cache.set(cache_key, content, expire=SCRAPE_CACHE_TTL_S)
        self._emitter.emit("scrape_complete", url=url, ok=True, content_chars=len(content))
        return content

    async def _recover_and_retry(self, url: str) -> str:
        try:
            await self._session.stop()
            await self._session.start()
            self._emitter.emit("browser_recovery", url=url, ok=True)
            return await self._fetch_via_browser(url)
        except Exception as exc:
            self._emitter.emit("browser_recovery", url=url, ok=False, error=str(exc))
            return await self._fetch_via_httpx(url)

    async def _fetch_via_browser(self, url: str) -> str:
        page = await self._session.new_tab()
        attach_page_listener(page, self._emitter)
        try:
            await page.goto(url, wait_until="networkidle", timeout=BROWSER_NAV_TIMEOUT_MS)
            text: str = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            return text
        finally:
            await page.close()

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(SETTINGS.http.retry_attempts),
        wait=wait_fixed(SETTINGS.http.retry_wait_s),
        reraise=True,
    )
    async def _fetch_via_httpx(self, url: str) -> str:
        response = await self._http.get(url)
        response.raise_for_status()
        return _html_to_text(response.text)

    async def aclose(self) -> None:
        await self._http.aclose()
