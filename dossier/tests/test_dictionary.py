from __future__ import annotations

import json

from dossier.engine.orchestrator import loop as orchestrator_loop
from dossier.engine.orchestrator.acronyms import extract_acronyms
from dossier.engine.orchestrator.actions import _format_senses, execute_actions
from dossier.engine.orchestrator.models import Actions, SearchAction
from dossier.engine.orchestrator.state import State
from dossier.engine.llm.mock import MockLLMClient
from dossier.engine.search.registry import Registry
from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.infra.cache.store import CacheStore
from dossier.infra.events.emitter import EventEmitter
from dossier.sources.dictionary import DictionaryClient


def test_extract_acronyms_finds_candidates_and_drops_stopwords() -> None:
    text = "How does SSO handle TTL for the RBAC service? See the API and JSON docs."
    found = extract_acronyms(text)
    assert {"SSO", "TTL", "RBAC"} <= found
    # ubiquitous tech terms are stoplisted
    assert "API" not in found and "JSON" not in found


class _FakeSource:
    def __init__(self, name: str, results: list[Result]) -> None:
        self._name = name
        self._results = results

    def name(self) -> str:
        return self._name

    def kind(self) -> str:
        return "fake"

    async def initialize(self) -> None:
        pass

    async def search(self, query: str, opts: Options) -> list[Result]:
        return self._results

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def lookup(self, term: str) -> Result | None:
        return None


class _FakeFetcher:
    async def fetch(self, url: str) -> str:
        return ""


class _FakeDictionary(DictionaryClient):
    """A configured dictionary that resolves from an in-memory map, no network.
    Each acronym maps to a list of senses, matching the real endpoint shape."""

    def __init__(self, defs: dict[str, list[str]]) -> None:
        super().__init__(base_url="https://lookup.test")
        self._defs = defs
        self.asked: list[str] = []

    async def define(self, acronym: str) -> list[str] | None:
        self.asked.append(acronym)
        return self._defs.get(acronym)


def test_dictionary_client_unconfigured_is_noop() -> None:
    assert DictionaryClient(base_url="").configured is False


async def test_enrichment_resolves_acronyms_from_question_and_results(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    registry = Registry(cache)
    registry.register(
        _FakeSource(
            "src",
            [Result(source="src", id="1", title="RBAC rollout", content="MFA is now required")],
        )
    )
    state = State(question="How does SSO work?")
    # One scored response for the single search result.
    llm = MockLLMClient(
        [json.dumps([{"source": "1", "direct_relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 8}])]
    )
    actions = Actions(searches=[SearchAction(source="src", query="q")])
    dictionary = _FakeDictionary(
        {"SSO": ["Single Sign-On"], "RBAC": ["Role-Based Access Control"], "MFA": ["Multi-Factor Auth"]}
    )

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter, dictionary=dictionary)

    # Acronyms from the question (SSO) and result title/content (RBAC, MFA) resolved.
    assert state.dictionary["SSO"] == "Single Sign-On"
    assert state.dictionary["RBAC"] == "Role-Based Access Control"
    assert state.dictionary["MFA"] == "Multi-Factor Auth"
    # They flow into the plan digest for later iterations.
    assert "SSO" in state.evidence_summary()

    # A second pass does not re-query already-checked acronyms.
    before = len(dictionary.asked)
    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter, dictionary=dictionary)
    assert dictionary.asked[before:] == []


class _NullFetcher:
    async def fetch(self, url: str) -> str:
        return ""


async def test_run_enriches_question_acronyms_before_first_plan(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # The question's own acronyms are resolved BEFORE the opening plan call, so
    # their definitions appear in the very first plan prompt (not just later
    # iterations). High confidence terminates after one plan call.
    registry = Registry(cache)
    registry.register(_FakeSource("src", []))
    llm = MockLLMClient(
        [
            json.dumps(
                {
                    "thinking": "t",
                    "confidence": {
                        "explicit_evidence": 13, "implicit_evidence": 13,
                        "evidence_consistency": 13, "answer_specificity": 13,
                    },
                    "actions": {"searches": [], "scrapes": [], "lookups": []},
                }
            ),
            json.dumps({"answer": "a", "citations": []}),
        ]
    )
    dictionary = _FakeDictionary({"SSO": ["Single Sign-On"], "RBAC": ["Role-Based Access Control"]})

    await orchestrator_loop.run(
        "How does SSO enforce RBAC?", registry, _NullFetcher(), llm, emitter, dictionary=dictionary
    )

    # Resolved before the plan ran (not after any search).
    assert dictionary.asked and "SSO" in dictionary.asked
    plan_call = next(c for c in llm.calls if c["request_type"] == "plan")
    assert "Single Sign-On" in plan_call["prompt"]
    assert "Role-Based Access Control" in plan_call["prompt"]


async def test_enrichment_no_dictionary_is_inert(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    registry = Registry(cache)
    registry.register(_FakeSource("src", []))
    state = State(question="What about SSO and RBAC?")
    llm = MockLLMClient([])
    actions = Actions(searches=[SearchAction(source="src", query="q")])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter, dictionary=None)

    assert state.dictionary == {}
    assert state.dictionary_checked == set()


def test_definitions_from_html_extracts_meaning_cells() -> None:
    import httpx

    from dossier.sources.dictionary.client import _definitions_from_response

    # acronymfinder.com-style results table: expansions live in cells whose
    # class contains "meaning", with inner links that must not truncate the text.
    html = """
    <table class="result-list">
      <tr>
        <td class="result-list__body__acronym">SSO</td>
        <td class="result-list__body__meaning"><a href="/x">Single Sign-On</a></td>
      </tr>
      <tr>
        <td class="result-list__body__meaning">Server Side Optimization (computing)</td>
      </tr>
    </table>
    """
    resp = httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
    senses = _definitions_from_response(resp)
    # Only the first (top-ranked) expansion is kept.
    assert senses == ["Single Sign-On"]


def test_html_body_without_meaning_cells_yields_nothing() -> None:
    import httpx

    from dossier.sources.dictionary.client import _definitions_from_response

    # A page with no "meaning" cells contributes no junk (not raw page chrome).
    resp = httpx.Response(
        200, text="<html><body><h1>Not found</h1></body></html>",
        headers={"content-type": "text/html"},
    )
    assert _definitions_from_response(resp) == []


def test_html_detected_even_without_content_type_header() -> None:
    import httpx

    from dossier.sources.dictionary.client import _definitions_from_response

    html = '<td class="meaning">Multi-Factor Authentication</td>'
    resp = httpx.Response(200, text=html)  # no content-type header
    assert _definitions_from_response(resp) == ["Multi-Factor Authentication"]


def test_json_endpoint_still_works_alongside_html() -> None:
    import httpx

    from dossier.sources.dictionary.client import _definitions_from_response

    resp = httpx.Response(200, json={"definitions": ["A", "B"]})
    assert _definitions_from_response(resp) == ["A", "B"]


async def test_define_is_graceful_when_blocked_or_missing() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if "SSO" in request.url.path:
            return httpx.Response(403, text="Forbidden")  # bot protection
        return httpx.Response(404)  # no match

    client = DictionaryClient(base_url="https://af.test")
    client._client = httpx.AsyncClient(
        base_url="https://af.test", transport=httpx.MockTransport(handler)
    )
    # Neither a 403 block nor a 404 miss raises - both resolve to None so
    # enrichment stays best-effort and quiet.
    assert await client.define("SSO") is None
    assert await client.define("XYZ") is None
    await client.aclose()


async def test_define_parses_live_html_result_page() -> None:
    import httpx

    # allacronyms.com-style markup: expansions in a "full_form" element.
    html = '<a class="a-result__full_form" href="/x">Single Sign-On</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        # Domain filter is appended: /{acronym}/{domain}.
        assert request.url.path == "/SSO/computing"
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    client = DictionaryClient(base_url="https://aa.test", domain="computing")
    client._client = httpx.AsyncClient(
        base_url="https://aa.test", transport=httpx.MockTransport(handler)
    )
    assert await client.define("SSO") == ["Single Sign-On"]
    await client.aclose()


async def test_empty_domain_appends_no_filter_segment() -> None:
    import httpx

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(404)

    client = DictionaryClient(base_url="https://aa.test", domain="")
    client._client = httpx.AsyncClient(
        base_url="https://aa.test", transport=httpx.MockTransport(handler)
    )
    await client.define("SSO")
    assert seen["path"] == "/SSO"  # no trailing domain segment
    await client.aclose()


def test_html_extraction_strips_count_and_arrow_chrome() -> None:
    import httpx

    from dossier.sources.dictionary.client import _definitions_from_response

    # allacronyms full-form element bundles the expansion with a "+N" alt-count
    # badge and an "Arrow" expand icon; only the expansion should survive.
    html = (
        '<div class="a-result__full_form">\n'
        '  <a href="/x">Role-Based Access Control</a>\n'
        '  <span class="count">+ 6</span>\n'
        '  <span class="icon">Arrow</span>\n'
        "</div>"
    )
    resp = httpx.Response(200, text=html, headers={"content-type": "text/html"})
    assert _definitions_from_response(resp) == ["Role-Based Access Control"]


async def test_client_sends_browser_user_agent() -> None:
    client = DictionaryClient(base_url="https://af.test")
    http = await client._ensure_client()
    assert "Mozilla" in http.headers["user-agent"]  # not python-httpx/*
    await client.aclose()


def test_coerce_definitions_handles_list_dict_and_text() -> None:
    from dossier.sources.dictionary.client import _coerce_definitions

    # The documented shape: a list of strings (expansions, paragraphs, links).
    assert _coerce_definitions(["Single Sign-On", "https://wiki/sso"]) == [
        "Single Sign-On",
        "https://wiki/sso",
    ]
    # Senses nested under a plural key, or a single scalar key.
    assert _coerce_definitions({"definitions": ["A", "B"]}) == ["A", "B"]
    assert _coerce_definitions({"definition": "Just one"}) == ["Just one"]
    # Plain string and junk-tolerance (blanks/non-strings dropped, entries trimmed).
    assert _coerce_definitions("plain") == ["plain"]
    assert _coerce_definitions([" x ", "", 5, "y"]) == ["x", "y"]
    assert _coerce_definitions({}) == []


def test_format_senses_single_is_bare_multiple_numbered_and_bounded() -> None:
    assert _format_senses(["Single Sign-On"]) == "Single Sign-On"

    multi = _format_senses(["Single Sign-On", "Screen Shot Only", "See https://wiki/sso"])
    assert "(1) Single Sign-On" in multi
    assert "(2) Screen Shot Only" in multi
    assert "wiki/sso" in multi

    # A long paragraph is truncated; overflow past the cap is summarized.
    many = _format_senses([f"sense-{i} " + "x" * 500 for i in range(7)])
    assert "…" in many
    assert "(+3 more)" in many  # 7 senses, 4 kept


async def test_enrichment_joins_multiple_senses_into_digest(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    registry = Registry(cache)
    registry.register(_FakeSource("src", []))
    state = State(question="What is SSO here?")
    llm = MockLLMClient([])
    actions = Actions(searches=[SearchAction(source="src", query="q")])
    dictionary = _FakeDictionary(
        {"SSO": ["Single Sign-On", "The internal auth gateway; see https://wiki/sso"]}
    )

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter, dictionary=dictionary)

    entry = state.dictionary["SSO"]
    assert "(1) Single Sign-On" in entry
    assert "wiki/sso" in entry
    assert "SSO" in state.evidence_summary()
