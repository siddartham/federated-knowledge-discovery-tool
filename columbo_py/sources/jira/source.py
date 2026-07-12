"""Jira source: real Jira Cloud REST API (JQL search), authenticated with the
same user-owned API token pattern as Confluence (email + token, Basic auth) -
Atlassian issues one token per user across both products. Uses httpx
directly rather than atlassian-python-api, which is a synchronous library
and would block the event loop.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import httpx

from columbo_py.config import SETTINGS
from columbo_py.engine.search.result import Options, Result
from columbo_py.engine.search.source import Capabilities
from columbo_py.sources.guidance import load_guidance

KIND_DESCRIPTION = load_guidance(__file__)

# Whether the planner wrote real JQL (any field operator present) vs bare
# keywords we should wrap in a text clause.
_JQL_HINT_RE = re.compile(r"(~|!=|>=|<=|=|>|<|\bAND\b|\bOR\b|\bNOT\b|\bIN\b|\bORDER BY\b)", re.I)


def _escape_jql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _looks_like_jql(query: str) -> bool:
    return bool(_JQL_HINT_RE.search(query))


class JiraSource:
    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        self._email = email or os.environ.get("JIRA_EMAIL")
        self._api_token = api_token or os.environ.get("JIRA_API_TOKEN")
        self._client: httpx.AsyncClient | None = None

    def name(self) -> str:
        return "jira"

    def kind(self) -> str:
        return KIND_DESCRIPTION

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=(), supports_days=True, supports_author=True, supports_scope=True, max_limit=100
        )

    async def initialize(self) -> None:
        if self._client is not None:
            return
        if not (self._base_url and self._email and self._api_token):
            raise RuntimeError(
                "Jira not configured: set JIRA_BASE_URL "
                "(e.g. https://yourcompany.atlassian.net), JIRA_EMAIL, and "
                "JIRA_API_TOKEN (generate one at "
                "https://id.atlassian.com/manage-profile/security/api-tokens)."
            )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(self._email, self._api_token),
            headers={"Accept": "application/json"},
            timeout=SETTINGS.http.default_timeout_s,
        )

    def _to_jql(self, query: str) -> str:
        """Pass the planner's query through as JQL. If it wrote bare keywords
        (no JQL operator), wrap them in a `text ~ "..."` clause. Append a
        default ordering only when the planner didn't specify one."""
        jql = query if _looks_like_jql(query) else f'text ~ "{_escape_jql_string(query)}"'
        if "order by" not in jql.lower():
            jql += " ORDER BY updated DESC"
        return jql

    async def search(self, query: str, opts: Options) -> list[Result]:
        if self._client is None:
            await self.initialize()
        assert self._client is not None

        response = await self._client.get(
            "/rest/api/3/search",
            params={
                "jql": self._to_jql(query),
                "maxResults": min(opts.limit, 100),
                "fields": "summary,description,status,updated,project,reporter",
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [self._issue_to_result(issue) for issue in payload.get("issues", [])]

    async def lookup(self, term: str) -> Result | None:
        """`term` is an issue key, e.g. "ENG-1234"."""
        if self._client is None:
            await self.initialize()
        assert self._client is not None

        response = await self._client.get(
            f"/rest/api/3/issue/{term}",
            params={"fields": "summary,description,status,updated,project,reporter"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._issue_to_result(response.json())

    def _issue_to_result(self, issue: dict[str, Any]) -> Result:
        fields = issue.get("fields", {})
        description = fields.get("description")
        content = _adf_to_text(description) if isinstance(description, dict) else (description or "")
        updated = fields.get("updated")
        return Result(
            source="jira",
            id=issue["key"],
            title=fields.get("summary", ""),
            content=content,
            timestamp=datetime.fromisoformat(updated) if updated else None,
            permalink=f"{self._base_url}/browse/{issue['key']}",
            metadata={
                "issue_number": issue["key"],
                "project": fields.get("project", {}).get("key"),
                "status": fields.get("status", {}).get("name"),
            },
        )


def _adf_to_text(node: dict[str, Any]) -> str:
    """Jira's Cloud API returns descriptions as Atlassian Document Format
    (a rich-text JSON tree), not plain text - walk it and join every text
    leaf, which is good enough for LLM consumption without reproducing full
    ADF rendering (tables, panels, etc.)."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            for child in n.get("content", []) or []:
                walk(child)
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return " ".join(parts)
