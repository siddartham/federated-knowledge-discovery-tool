from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from columbo_py.engine.search.registry import Registry
from columbo_py.engine.search.result import Options
from columbo_py.engine.search.source import LookupCapableSource, SearchSource
from columbo_py.infra.cache.store import CacheStore
from columbo_py.sources.confluence import ConfluenceSource
from columbo_py.sources.drive import DriveSource
from columbo_py.sources.github import GitHubCodeSource, GitHubIssuesSource, GitHubSource
from columbo_py.sources.jira import JiraSource
from columbo_py.sources.slack import SlackSource
from columbo_py.sources.slack.auth import SlackCredentials


def test_all_sources_satisfy_search_source_protocol() -> None:
    sources: list[SearchSource] = [
        GitHubCodeSource(token="t"),
        GitHubIssuesSource(token="t"),
        SlackSource(credentials=None),
        ConfluenceSource(base_url="https://x.atlassian.net/wiki", email="a@b.com", api_token="t"),
        JiraSource(base_url="https://x.atlassian.net", email="a@b.com", api_token="t"),
        DriveSource(client_secrets_path=Path("/tmp/secrets.json")),
    ]
    for source in sources:
        assert isinstance(source, SearchSource), f"{source.name()} fails SearchSource"
        assert isinstance(source, LookupCapableSource), f"{source.name()} fails LookupCapableSource"
        assert source.name()
        assert source.kind()
        assert source.capabilities() is not None


def test_source_names_are_unique() -> None:
    sources: list[SearchSource] = [
        GitHubCodeSource(),
        GitHubIssuesSource(),
        SlackSource(),
        ConfluenceSource(),
        JiraSource(),
        DriveSource(),
    ]
    names = [s.name() for s in sources]
    assert len(names) == len(set(names))


def test_github_source_alias_is_issues() -> None:
    # Backward-compatible alias must keep the original issue/PR behavior.
    assert GitHubSource is GitHubIssuesSource
    assert GitHubIssuesSource().name() == "github_issues"
    assert GitHubCodeSource().name() == "github_code"


def test_kind_returns_full_guidance() -> None:
    # kind() now carries the per-source GUIDANCE.md, not a one-liner.
    assert "code search" in GitHubCodeSource(token="t").kind().lower()
    assert "CQL" in ConfluenceSource(base_url="https://x/wiki", email="a@b.com", api_token="t").kind()
    assert "JQL" in JiraSource(base_url="https://x", email="a@b.com", api_token="t").kind()
    assert "Best for" in DriveSource(client_secrets_path=Path("/tmp/s.json")).kind()


def test_confluence_passthrough_and_fallback() -> None:
    source = ConfluenceSource(base_url="https://x.atlassian.net/wiki", email="a@b.com", api_token="t")
    # Bare keywords get wrapped as a text clause.
    assert source._to_cql("release process") == 'text ~ "release process"'
    # A planner-written CQL query is passed through untouched.
    cql = 'text ~ "kafka" AND space = "ENG" ORDER BY lastModified DESC'
    assert source._to_cql(cql) == cql


def test_jira_passthrough_and_fallback() -> None:
    source = JiraSource(base_url="https://x.atlassian.net", email="a@b.com", api_token="t")
    # Bare keywords -> text clause + default ordering.
    jql = source._to_jql("login bug")
    assert jql == 'text ~ "login bug" ORDER BY updated DESC'
    # Planner JQL passed through; default ordering appended (none specified).
    assert source._to_jql('project = "ENG" AND type = Bug').endswith("ORDER BY updated DESC")
    # A planner-specified ORDER BY is not doubled.
    explicit = 'text ~ "x" ORDER BY created DESC'
    assert source._to_jql(explicit) == explicit


def test_drive_query_building() -> None:
    source = DriveSource(client_secrets_path=Path("/tmp/secrets.json"))
    query = source._build_query("quarterly plan", Options(scope="folder123", days=7))
    assert "fullText contains 'quarterly plan'" in query
    assert "trashed = false" in query
    assert "'folder123' in parents" in query
    assert "modifiedTime >=" in query


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSlackHTTP:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResp:
        self.calls.append(params or {})
        return _FakeResp(self._pages[len(self.calls) - 1])


def test_slack_lookup_follows_pagination_cursor() -> None:
    # A channel on the second page must still be found - a single-page
    # conversations.list would silently miss it in a large workspace.
    import asyncio

    source = SlackSource(credentials=SlackCredentials(token="xoxc-x", d_cookie="d", team_id=None))
    fake = _FakeSlackHTTP(
        [
            {"ok": True, "channels": [{"name": "general", "id": "C1"}],
             "response_metadata": {"next_cursor": "c2"}},
            {"ok": True, "channels": [{"name": "platform", "id": "C2",
             "purpose": {"value": "platform team"}}],
             "response_metadata": {"next_cursor": ""}},
        ]
    )
    source._client = fake  # type: ignore[assignment]

    result = asyncio.run(source.lookup("#platform"))

    assert result is not None
    assert result.id == "C2"
    assert result.content == "platform team"
    # Two pages fetched; the second carried the cursor from the first.
    assert len(fake.calls) == 2
    assert fake.calls[1].get("cursor") == "c2"
    # Token is never sent as a query param (it's an Authorization header now).
    assert all("token" not in c for c in fake.calls)


def test_slack_native_oauth_uses_cached_token(tmp_path: Path, monkeypatch: Any) -> None:
    # With a Slack app configured and a token already cached on disk, the
    # native OAuth path returns the xoxp- token and never touches the browser.
    import asyncio
    import json

    token_file = tmp_path / "slack_token.json"
    token_file.write_text(json.dumps({"access_token": "xoxp-cached", "team_id": "T1"}))
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")

    source = SlackSource(token_path=token_file)
    creds = asyncio.run(source._resolve_credentials())

    assert creds.token == "xoxp-cached"
    # A native user token authenticates on its own - no `d` cookie.
    assert creds.d_cookie is None


def test_slack_credential_preference_order(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    # Manual xoxp- token (no OAuth app, no cookie) is accepted cookie-free.
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SLACK_D_COOKIE", raising=False)
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-manual")
    creds = asyncio.run(SlackSource(token_path=tmp_path / "t.json")._resolve_credentials())
    assert creds.token == "xoxp-manual"
    assert creds.d_cookie is None

    # An xoxc- browser token still carries its `d` cookie.
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxc-manual")
    monkeypatch.setenv("SLACK_D_COOKIE", "dcookie")
    creds = asyncio.run(SlackSource(token_path=tmp_path / "t.json")._resolve_credentials())
    assert creds.token == "xoxc-manual"
    assert creds.d_cookie == "dcookie"

    # Nothing configured and no browser → a clear, actionable error.
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_D_COOKIE", raising=False)
    with pytest.raises(RuntimeError, match="no Slack credentials"):
        asyncio.run(SlackSource(token_path=tmp_path / "t.json")._resolve_credentials())


class _FakeHTTP:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResp:
        self.calls.append((path, params or {}))
        return _FakeResp(self._payload)


def test_github_code_search_passes_query_through_and_maps_results() -> None:
    import asyncio

    source = GitHubCodeSource(token="t")
    payload = {
        "items": [
            {
                "path": "src/auth.py",
                "repository": {"full_name": "acme/auth"},
                "html_url": "https://github.com/acme/auth/blob/main/src/auth.py",
                "text_matches": [{"fragment": "ACCESS_TOKEN_TTL=3600"}],
            }
        ]
    }
    fake = _FakeHTTP(payload)
    source._gh._client = fake  # type: ignore[assignment]

    results = asyncio.run(source.search("ACCESS_TOKEN_TTL repo:acme/auth", Options(limit=10)))

    assert len(results) == 1
    r = results[0]
    assert r.source == "github_code"
    assert r.id == "acme/auth:src/auth.py"
    assert "ACCESS_TOKEN_TTL=3600" in r.content
    assert r.permalink is not None and r.permalink.endswith("src/auth.py")
    # The planner's native code-search query is passed through verbatim.
    assert fake.calls[0][0] == "/search/code"
    assert fake.calls[0][1]["q"] == "ACCESS_TOKEN_TTL repo:acme/auth"


def test_sources_prompt_block_carries_keys_and_guidance(cache: CacheStore) -> None:
    registry = Registry(cache)
    registry.register(GitHubCodeSource(token="t"))
    registry.register(ConfluenceSource(base_url="https://x/wiki", email="a@b.com", api_token="t"))
    block = registry.sources_prompt_block()
    assert '### source key: "github_code"' in block
    assert '### source key: "confluence"' in block
    assert "code search" in block.lower()
    assert "CQL" in block


def test_github_missing_token_raises_clear_error() -> None:
    import asyncio

    source = GitHubIssuesSource(token=None)
    source._gh._token = None  # ignore any GITHUB_TOKEN in the test environment
    try:
        asyncio.run(source.initialize())
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "GITHUB_TOKEN" in str(exc)
