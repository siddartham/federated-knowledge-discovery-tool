"""GitHub sources: two search modes over the GitHub REST API, both
authenticated with a user-owned personal access token (`GITHUB_TOKEN`) - not a
service account or app-installation token, satisfying the "auth w/o
provisioning tokens" goal.

They're registered as two distinct sources so the planner picks by name:

- `github_code`   -> /search/code   : file contents, config, implementation
- `github_issues` -> /search/issues : issues, PRs, discussion/decision threads

Both use httpx directly rather than PyGithub, which is synchronous and would
block the event loop during the orchestrator's parallel fan-out.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from dossier.config import SETTINGS
from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.sources.guidance import load_guidance

GITHUB_API_BASE = SETTINGS.github.api_base
CODE_KIND = load_guidance(__file__, "GUIDANCE_CODE.md")
ISSUES_KIND = load_guidance(__file__, "GUIDANCE_ISSUES.md")


def _parse_github_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _GitHubClient:
    """Shared lazy-auth httpx client for both GitHub search modes."""

    def __init__(self, token: str | None = None, accept: str = "application/vnd.github+json") -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self._accept = accept
        self._client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        if not self._token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set. Generate a personal access token "
                "(classic or fine-grained, `repo` + `read:org` scopes) at "
                "https://github.com/settings/tokens and export it."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API_BASE,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": self._accept,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=SETTINGS.http.default_timeout_s,
            )

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.initialize()
        assert self._client is not None
        return self._client


class GitHubCodeSource:
    """Code search: file contents and paths, with scrapable file URLs. The
    planner writes native GitHub code-search syntax (qualifiers like `repo:`,
    `path:`, `language:`); it's passed through to /search/code verbatim."""

    def __init__(self, token: str | None = None) -> None:
        # text-match accept header asks GitHub for the matching code fragments
        # so results carry a usable snippet, not just a file path.
        self._gh = _GitHubClient(token, accept="application/vnd.github.text-match+json")

    def name(self) -> str:
        return "github_code"

    def kind(self) -> str:
        return CODE_KIND

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=("repo:", "org:", "user:", "language:", "path:", "symbol:", "content:"),
            supports_days=False,
            supports_author=False,
            supports_scope=False,
            max_limit=100,
        )

    async def initialize(self) -> None:
        await self._gh.initialize()

    async def search(self, query: str, opts: Options) -> list[Result]:
        client = await self._gh.client()
        response = await client.get(
            "/search/code", params={"q": query, "per_page": min(opts.limit, 100)}
        )
        response.raise_for_status()
        payload = response.json()
        return [self._code_to_result(item) for item in payload.get("items", [])]

    async def lookup(self, term: str) -> Result | None:
        """`term` is "owner/repo:path/to/file" - fetch that file directly."""
        if ":" not in term or "/" not in term.split(":", 1)[0]:
            return None
        repo, _, path = term.partition(":")
        client = await self._gh.client()
        response = await client.get(f"/repos/{repo}/contents/{path}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        item = response.json()
        return Result(
            source="github_code",
            id=f"{repo}:{path}",
            title=path,
            content=item.get("name", path),
            permalink=item.get("html_url"),
            metadata={"repository": repo, "path": path},
        )

    @staticmethod
    def _code_to_result(item: dict[str, Any]) -> Result:
        repo = item.get("repository", {}).get("full_name", "")
        path = item.get("path", "")
        fragments = [m.get("fragment", "") for m in item.get("text_matches", [])]
        return Result(
            source="github_code",
            id=f"{repo}:{path}",
            title=path,
            content="\n---\n".join(f for f in fragments if f),
            permalink=item.get("html_url"),
            metadata={"repository": repo, "path": path},
        )


class GitHubIssuesSource:
    """Issue/PR search: titles, bodies, and comments. The planner writes
    GitHub issue-search qualifiers (`is:pr`, `label:`, `author:` …), passed
    through to /search/issues verbatim."""

    def __init__(self, token: str | None = None) -> None:
        self._gh = _GitHubClient(token)

    def name(self) -> str:
        return "github_issues"

    def kind(self) -> str:
        return ISSUES_KIND

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=("repo:", "org:", "is:issue", "is:pr", "author:", "label:", "in:"),
            supports_days=True,
            supports_author=True,
            supports_scope=False,
            max_limit=100,
        )

    async def initialize(self) -> None:
        await self._gh.initialize()

    async def search(self, query: str, opts: Options) -> list[Result]:
        client = await self._gh.client()
        response = await client.get(
            "/search/issues", params={"q": query, "per_page": min(opts.limit, 100)}
        )
        response.raise_for_status()
        payload = response.json()
        return [self._issue_to_result(item) for item in payload.get("items", [])]

    async def lookup(self, term: str) -> Result | None:
        """`term` is "owner/repo#123" - a direct issue/PR lookup by number."""
        if "#" not in term or "/" not in term.split("#", 1)[0]:
            return None
        repo, _, number = term.partition("#")
        client = await self._gh.client()
        response = await client.get(f"/repos/{repo}/issues/{number}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._issue_to_result(response.json())

    @staticmethod
    def _issue_to_result(item: dict[str, Any]) -> Result:
        repo_full_name = item.get("repository_url", "").rsplit("/repos/", 1)[-1]
        return Result(
            source="github_issues",
            id=str(item["number"]),
            title=item.get("title", ""),
            content=item.get("body") or "",
            timestamp=_parse_github_timestamp(item.get("updated_at")),
            permalink=item.get("html_url"),
            metadata={
                "repository": repo_full_name,
                "issue_number": item.get("number"),
                "state": item.get("state"),
                "is_pull_request": "pull_request" in item,
            },
        )


# Backward-compatible alias: the original single source did issue/PR search.
GitHubSource = GitHubIssuesSource
