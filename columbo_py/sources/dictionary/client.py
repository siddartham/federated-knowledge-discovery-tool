"""Acronym/abbreviation dictionary client: resolves an abbreviation to its
definition via `GET {base}/{abbreviation}/{domain}` (default
`https://www.allacronyms.com/{abbreviation}/computing`). The trailing domain
segment restricts results to one field so an acronym gets its computing sense.

Unlike the search sources, this isn't queried by the planner directly - the
orchestrator auto-detects acronyms in the question and in search-result text
and resolves them here, feeding the definitions back into the plan digest so
later iterations understand the jargon (see `actions.enrich_acronyms`).

Response handling adapts to what the endpoint returns: an HTML results page
(allacronyms.com's format - expansions are parsed out of the full-form
elements), a JSON list/dict of senses, or plain text. See
`_definitions_from_response`.

Configured via env:
- COLUMBO_DICTIONARY_URL    base URL (default https://www.allacronyms.com);
                            unset/empty disables enrichment (client is a no-op).
- COLUMBO_DICTIONARY_DOMAIN category segment (default "computing"); empty appends
                            no segment, querying every field.
- COLUMBO_DICTIONARY_TOKEN  optional bearer token for the endpoint.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from columbo_py.config import SETTINGS

DEFAULT_DICTIONARY_URL = SETTINGS.dictionary.url

# A realistic browser User-Agent: public dictionary sites (allacronyms.com
# included) reject the default `python-httpx/...` agent with 403. `Accept`
# favors HTML (the default site) but still allows JSON for internal endpoints.
# Both configured in config/defaults.toml ([dictionary]).
_USER_AGENT = SETTINGS.dictionary.user_agent
_ACCEPT = SETTINGS.dictionary.accept

# allacronyms.com supports a trailing category segment (`/{abbr}/{domain}`) that
# restricts results to one field, so an acronym gets its *computing* sense
# rather than every unrelated expansion. Overridable; empty disables the filter.
DEFAULT_DICTIONARY_DOMAIN = SETTINGS.dictionary.domain


class DictionaryClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        domain: str | None = None,
    ) -> None:
        # DEFAULT_DICTIONARY_URL/DOMAIN already fold in COLUMBO_DICTIONARY_URL/
        # DOMAIN via the central config; an explicit arg still wins. The bearer
        # token is a secret, so it stays read straight from the environment.
        raw = base_url if base_url is not None else DEFAULT_DICTIONARY_URL
        self._base_url = raw.rstrip("/")
        self._token = token or os.environ.get("COLUMBO_DICTIONARY_TOKEN")
        raw_domain = domain if domain is not None else DEFAULT_DICTIONARY_DOMAIN
        self._domain = raw_domain.strip().strip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=SETTINGS.http.dictionary_timeout_s,
                follow_redirects=True,
            )
        return self._client

    async def define(self, acronym: str) -> list[str] | None:
        """Return the dictionary's definition(s) for `acronym`, or None if
        there's no usable entry (no match, or the endpoint is unavailable).

        The dictionary is a best-effort enhancement, never load-bearing: an
        unreachable or blocking endpoint (404 no-match, 403 bot-protection, 429,
        5xx, timeouts) resolves to None so enrichment simply contributes nothing
        rather than raising or spamming errors into the run.

        The endpoint may hold several senses for one acronym - a bare expansion,
        a free-form paragraph, a link, or a mix - so this returns a list of every
        non-empty sense in order. The caller decides how to present them (see
        `actions._format_senses`)."""
        if not self.configured:
            return None
        client = await self._ensure_client()
        path = f"/{acronym}/{self._domain}" if self._domain else f"/{acronym}"
        try:
            response = await client.get(path)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        return _definitions_from_response(response) or None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _definitions_from_response(response: httpx.Response) -> list[str]:
    """Normalize whatever the endpoint returns into a flat list of non-empty
    sense strings. Tolerates: an HTML results page (allacronyms.com - the
    default), a JSON list of strings, a plain string, or a dict nesting the
    senses under a list/scalar key."""
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return _definitions_from_html(response.text)
    try:
        data: Any = response.json()
    except ValueError:
        text = response.text.strip()
        # Non-JSON with no HTML content-type: an HTML body from a server that
        # didn't set the header still shouldn't leak raw markup into the digest.
        if text.startswith("<"):
            return _definitions_from_html(text)
        return [text] if text else []
    return _coerce_definitions(data)


# Public acronym sites render each expansion in an element whose class hints at
# "the full form": allacronyms.com uses `full_form`/`full-form`, acronymfinder
# uses `...__meaning`. Substring-matching these hints keeps extraction working
# across sites and minor markup revisions without hardcoding a full class name.
_DEFINITION_CLASS_HINTS = ("full_form", "full-form", "fullform", "meaning", "definition")
# Public acronym sites rank results by popularity, so the first full-form element
# is the top/most-common expansion. We keep only that one - the single best
# guess for the acronym - rather than every listed sense. From
# config/defaults.toml ([dictionary].max_html_senses).
_MAX_HTML_SENSES = SETTINGS.dictionary.max_html_senses


# The full-form element bundles the expansion with UI chrome: a "+N" badge (how
# many other expansions exist) and an expand-icon whose text reads "Arrow", e.g.
# "Role-Based Access Control + 6 Arrow". Strip that trailing count/icon noise so
# only the expansion remains. Requires whitespace before "+" so "C++11"-style
# text is left alone.
_HTML_NOISE_RE = re.compile(r"(\s+\+\s*\d.*| Arrow)\s*$")


def _clean_definition(text: str) -> str:
    prev = ""
    while prev != text:
        prev = text
        text = _HTML_NOISE_RE.sub("", text).strip()
    return text


def _definitions_from_html(html: str) -> list[str]:
    """Extract the first (top-ranked) acronym expansion from an HTML results
    page, as a one-item list; [] if none is found, so a non-matching page
    contributes nothing rather than dumping page chrome. Trailing UI chrome
    (the "+N" alt-count badge and "Arrow" icon text) is stripped."""
    parser = _MeaningExtractor()
    parser.feed(html)
    out: list[str] = []
    for meaning in parser.meanings:
        cleaned = _clean_definition(meaning)
        if cleaned:
            out.append(cleaned)
            if len(out) >= _MAX_HTML_SENSES:
                break
    return out


class _MeaningExtractor(HTMLParser):
    """Collects the text content of every element whose `class` attribute hints
    at an acronym expansion (see `_DEFINITION_CLASS_HINTS`). Tracks nesting depth
    so inner tags (links, spans) don't prematurely close the element."""

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0  # >0 while inside a definition element
        self._buffer: list[str] = []
        self.meanings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth:
            self._depth += 1
            return
        classes = next((v or "" for k, v in attrs if k == "class"), "").lower()
        if any(hint in classes for hint in _DEFINITION_CLASS_HINTS):
            self._depth = 1
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            text = " ".join("".join(self._buffer).split())
            if text:
                self.meanings.append(text)

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)


def _coerce_definitions(data: Any) -> list[str]:
    if isinstance(data, str):
        text = data.strip()
        return [text] if text else []
    if isinstance(data, list):
        return [item.strip() for item in data if isinstance(item, str) and item.strip()]
    if isinstance(data, dict):
        # Senses nested under a plural key (a list) ...
        for key in ("definitions", "meanings", "values", "senses", "results"):
            if key in data:
                return _coerce_definitions(data[key])
        # ... or a single sense under a scalar key.
        for key in ("definition", "meaning", "value"):
            if data.get(key):
                return _coerce_definitions(data[key])
    return []
