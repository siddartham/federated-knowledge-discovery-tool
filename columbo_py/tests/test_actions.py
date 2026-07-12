from __future__ import annotations

import json

import pytest

from columbo_py.engine.orchestrator.actions import execute_actions, should_skip_url
from columbo_py.engine.orchestrator.cost import CostTracker
from columbo_py.engine.orchestrator.models import Actions, SearchAction
from columbo_py.engine.orchestrator.state import State, select_evidence
from columbo_py.engine.llm.mock import MockLLMClient
from columbo_py.engine.search.registry import Registry
from columbo_py.engine.search.result import Options, Result
from columbo_py.engine.search.source import Capabilities
from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.events.emitter import EventEmitter


class _FakeSource:
    def __init__(self, name: str, results: list[Result]) -> None:
        self._name = name
        self._results = results
        self.initialized = False

    def name(self) -> str:
        return self._name

    def kind(self) -> str:
        return "a fake source"

    async def initialize(self) -> None:
        self.initialized = True

    async def search(self, query: str, opts: Options) -> list[Result]:
        return self._results

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def lookup(self, term: str) -> Result | None:
        for r in self._results:
            if r.id == term:
                return r
        return None


class _FailingSource(_FakeSource):
    async def search(self, query: str, opts: Options) -> list[Result]:
        raise RuntimeError("upstream is down")

    async def lookup(self, term: str) -> Result | None:
        raise RuntimeError("upstream is down")


class _FakeFetcher:
    def __init__(self, content: str = "scraped page body") -> None:
        self.content = content
        self.fetched_urls: list[str] = []

    async def fetch(self, url: str) -> str:
        self.fetched_urls.append(url)
        return self.content


@pytest.mark.parametrize(
    "url,already_scraped,expected_skip",
    [
        ("https://example.com/page", set(), False),
        ("javascript:alert(1)", set(), True),
        ("https://example.com/file.pdf", set(), True),
        ("https://example.com/x", {"https://example.com/x"}, True),
        ("https://" + "a" * 3000, set(), True),
        # Regex skip set: open-in-app / meeting / auth-walled hosts.
        ("https://app.slack.com/client/T1/C1", set(), True),
        ("https://acme.slack.com/archives/C1/p123", set(), True),
        ("https://slack.com/app_redirect?channel=C1", set(), True),
        ("https://us02web.zoom.us/j/8412345678", set(), True),
        ("https://teams.microsoft.com/l/meetup-join/xyz", set(), True),
        ("https://meet.google.com/abc-defg-hij", set(), True),
        ("https://acme.workday.com/foo", set(), True),
        ("https://mail.google.com/mail/u/0", set(), True),
        # Not skipped: an ordinary Google Doc / GitHub page.
        ("https://docs.google.com/document/d/abc/edit", set(), False),
        ("https://github.com/acme/repo/blob/main/README.md", set(), False),
    ],
)
def test_should_skip_url(url: str, already_scraped: set[str], expected_skip: bool) -> None:
    assert should_skip_url(url, already_scraped) == expected_skip


async def test_execute_actions_search_and_score(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    source = _FakeSource("github", [Result(source="github", id="1", title="T", content="C")])
    registry.register(source)

    state = State(question="what is x?")
    llm = MockLLMClient(
        [json.dumps([{"source": "1", "relevance": 13, "answer_potential": 8, "context_value": 5, "source_quality": 8}])]
    )
    actions = Actions(searches=[SearchAction(source="github", query="x")])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert source.initialized
    assert len(state.batches) == 1
    assert "1" in state.scored_results
    assert state.stats.searches == 1
    assert state.stats.results == 1


async def test_execute_actions_records_score_cost(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # The scoring Haiku call must accrue into the cost budget - previously
    # only plan/synthesis calls were metered, leaving a hole in cost control.
    registry = Registry(cache)
    registry.register(_FakeSource("github", [Result(source="github", id="1", title="T", content="C")]))
    state = State(question="q")
    llm = MockLLMClient(
        [json.dumps([{"source": "1", "relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 8}])]
    )
    actions = Actions(searches=[SearchAction(source="github", query="x")])
    cost = CostTracker(max_cost_usd=2.0)

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter, cost=cost)

    assert cost.spent_usd > 0.0


async def test_execute_actions_score_out_of_range_does_not_crash(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # An out-of-range score (47 > 13) for one row must skip that row, not
    # raise a ValidationError out of the scoring gather and crash the run.
    registry = Registry(cache)
    registry.register(
        _FakeSource(
            "github",
            [
                Result(source="github", id="1", title="T1", content="C1"),
                Result(source="github", id="2", title="T2", content="C2"),
            ],
        )
    )
    state = State(question="q")
    llm = MockLLMClient(
        [
            json.dumps(
                [
                    {"source": "1", "relevance": 47, "answer_potential": 8, "context_value": 5, "source_quality": 8},
                    {"source": "2", "relevance": 8, "answer_potential": 8, "context_value": 5, "source_quality": 8},
                ]
            )
        ]
    )
    actions = Actions(searches=[SearchAction(source="github", query="x")])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    # Bad row skipped, good row still scored.
    assert "1" not in state.scored_results
    assert "2" in state.scored_results


async def test_execute_actions_score_json_in_code_fence(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # A score response wrapped in a markdown fence must still parse (uses
    # extract_json now, not bare json.loads).
    registry = Registry(cache)
    registry.register(_FakeSource("github", [Result(source="github", id="1", title="T", content="C")]))
    state = State(question="q")
    fenced = "```json\n" + json.dumps(
        [{"source": "1", "relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 8}]
    ) + "\n```"
    llm = MockLLMClient([fenced])
    actions = Actions(searches=[SearchAction(source="github", query="x")])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert "1" in state.scored_results


async def test_execute_actions_search_error_does_not_raise(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_FailingSource("flaky", []))

    state = State(question="q")
    llm = MockLLMClient([])
    actions = Actions(searches=[SearchAction(source="flaky", query="x")])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert state.batches == []
    assert any("error" in a for a in state.actions_taken)


async def test_execute_actions_scrape(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    state = State(question="q")
    fetcher = _FakeFetcher("line one\nline two")
    llm = MockLLMClient(
        [json.dumps({"chunk_start": 1, "chunk_end": 2, "direct_relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 8})]
    )
    actions = Actions(scrapes=["https://example.com/doc"])

    await execute_actions(state, actions, registry, fetcher, llm, emitter)

    assert fetcher.fetched_urls == ["https://example.com/doc"]
    assert len(state.scrapes) == 1
    assert "https://example.com/doc" in state.scored_scrapes


async def test_execute_actions_scrape_denied_by_domain_gate(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # A gate that denies the host must stop the URL from ever reaching the fetcher.
    from columbo_py.infra.domaingate import DomainGate

    registry = Registry(cache)
    state = State(question="q")
    fetcher = _FakeFetcher("body")
    llm = MockLLMClient([])
    actions = Actions(scrapes=["https://unknown.example/doc"])
    gate = DomainGate(prompt=lambda h: "n")

    await execute_actions(state, actions, registry, fetcher, llm, emitter, gate=gate)

    assert fetcher.fetched_urls == []
    assert state.scrapes == []


async def test_execute_actions_scrape_allowed_by_domain_gate(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    from columbo_py.infra.domaingate import DomainGate

    registry = Registry(cache)
    state = State(question="q")
    fetcher = _FakeFetcher("line one\nline two")
    llm = MockLLMClient(
        [json.dumps({"chunk_start": 1, "chunk_end": 2, "direct_relevance": 8, "answer_potential": 8, "context_value": 8, "source_quality": 8})]
    )
    actions = Actions(scrapes=["https://approved.example/doc"])
    gate = DomainGate(allow=["approved.example"], prompt=lambda h: "n")

    await execute_actions(state, actions, registry, fetcher, llm, emitter, gate=gate)

    assert fetcher.fetched_urls == ["https://approved.example/doc"]
    assert len(state.scrapes) == 1


async def test_execute_actions_scrape_skipped_by_regex(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # A meeting/open-in-app URL is dropped by the skip filter before the gate.
    from columbo_py.infra.domaingate import DomainGate

    registry = Registry(cache)
    state = State(question="q")
    fetcher = _FakeFetcher("body")
    llm = MockLLMClient([])
    actions = Actions(scrapes=["https://meet.google.com/abc-defg-hij"])
    gate = DomainGate(prompt=lambda h: (_ for _ in ()).throw(AssertionError("gate should not run")))

    await execute_actions(state, actions, registry, fetcher, llm, emitter, gate=gate)

    assert fetcher.fetched_urls == []


async def test_execute_actions_lookup(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_FakeSource("jira", [Result(source="jira", id="ENG-1", title="T", content="C")]))

    state = State(question="q")
    # One scored response for the lookup hit (lookups are now scored like
    # search results so they can reach synthesis).
    llm = MockLLMClient(
        [json.dumps([{"source": "ENG-1", "relevance": 13, "answer_potential": 13, "context_value": 8, "source_quality": 8}])]
    )
    actions = Actions(lookups=["ENG-1"])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert state.dictionary["ENG-1"] == "C"
    assert state.stats.lookups == 1
    # The lookup Result is retained and scored, so it becomes citable evidence.
    assert [r.id for r in state.lookups] == ["ENG-1"]
    assert "ENG-1" in state.scored_results
    evidence = select_evidence(state)
    assert any(e.id == "ENG-1" and e.origin == "lookup" for e in evidence)


async def test_execute_actions_lookup_error_does_not_raise(
    cache: CacheStore, emitter: EventEmitter
) -> None:
    # A raising lookup must not escape the fan-out gather and crash the run
    # (or cancel sibling searches/scrapes).
    registry = Registry(cache)
    registry.register(_FailingSource("flaky", []))

    state = State(question="q")
    llm = MockLLMClient([])
    actions = Actions(lookups=["ENG-1"])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert state.stats.lookups == 0
    assert any("lookup" in a and "error" in a for a in state.actions_taken)


async def test_execute_actions_lookup_not_found(cache: CacheStore, emitter: EventEmitter) -> None:
    registry = Registry(cache)
    registry.register(_FakeSource("jira", []))

    state = State(question="q")
    llm = MockLLMClient([])
    actions = Actions(lookups=["ENG-999"])

    await execute_actions(state, actions, registry, _FakeFetcher(), llm, emitter)

    assert state.dictionary == {}
    assert state.stats.lookups == 0
