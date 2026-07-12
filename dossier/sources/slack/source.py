"""Slack source: real `search.messages` API calls authenticated with a
user-scoped token. Credentials are resolved in preference order (see
`initialize`): native OAuth user token (SLACK_CLIENT_ID/SECRET) first, then a
manually supplied token (SLACK_USER_TOKEN, with SLACK_D_COOKIE only if it's an
xoxc- browser token), and finally live browser interception as a fallback.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from dossier.config import SETTINGS
from dossier.engine.search.result import Options, Result
from dossier.engine.search.source import Capabilities
from dossier.infra.browser.session import BrowserSession
from dossier.sources.guidance import load_guidance
from dossier.sources.slack.auth import (
    DEFAULT_SLACK_TOKEN_PATH,
    SlackCredentials,
    get_user_token,
    intercept_slack_token,
)

SLACK_API_BASE = SETTINGS.slack.api_base
_MAX_CHANNEL_LIST_PAGES = SETTINGS.slack.max_channel_list_pages  # bounds a pathological workspace
KIND_DESCRIPTION = load_guidance(__file__)


class SlackSource:
    def __init__(
        self,
        browser_session: BrowserSession | None = None,
        credentials: SlackCredentials | None = None,
        token_path: Path = DEFAULT_SLACK_TOKEN_PATH,
    ) -> None:
        self._browser_session = browser_session
        self._credentials = credentials
        self._token_path = token_path
        self._client: httpx.AsyncClient | None = None

    def name(self) -> str:
        return "slack"

    def kind(self) -> str:
        return KIND_DESCRIPTION

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=("in:", "from:", "before:", "after:", "has:"),
            supports_days=True,
            supports_author=True,
            supports_scope=True,
            max_limit=100,
        )

    async def initialize(self) -> None:
        if self._client is not None:
            return

        creds = self._credentials
        if creds is None:
            creds = await self._resolve_credentials()
        self._credentials = creds
        # Pass the token in the Authorization header rather than as a URL query
        # param: query strings leak into access logs, proxies, and the event
        # log, and this token is a live user session credential. The `d` cookie
        # is only sent for xoxc- browser tokens; native xoxp- tokens authenticate
        # on their own (cookies=None).
        self._client = httpx.AsyncClient(
            base_url=SLACK_API_BASE,
            cookies={"d": creds.d_cookie} if creds.d_cookie else None,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=SETTINGS.http.default_timeout_s,
        )

    async def _resolve_credentials(self) -> SlackCredentials:
        """Preference order: native OAuth user token, then a manually supplied
        token, then live browser interception."""
        client_id = os.environ.get("SLACK_CLIENT_ID")
        client_secret = os.environ.get("SLACK_CLIENT_SECRET")
        if client_id and client_secret:
            token = await get_user_token(client_id, client_secret, self._token_path)
            return SlackCredentials(token=token, d_cookie=None, team_id=None)

        manual_token = os.environ.get("SLACK_USER_TOKEN")
        if manual_token:
            # An xoxp- token needs no cookie; an xoxc- token needs SLACK_D_COOKIE.
            return SlackCredentials(
                token=manual_token, d_cookie=os.environ.get("SLACK_D_COOKIE"), team_id=None
            )

        if self._browser_session is not None:
            return await intercept_slack_token(self._browser_session)

        raise RuntimeError(
            "no Slack credentials available: set SLACK_CLIENT_ID + "
            "SLACK_CLIENT_SECRET for the native OAuth flow, or SLACK_USER_TOKEN "
            "(plus SLACK_D_COOKIE for an xoxc- token), or construct SlackSource "
            "with a BrowserSession already logged into Slack"
        )

    async def search(self, query: str, opts: Options) -> list[Result]:
        if self._client is None:
            await self.initialize()
        assert self._client is not None and self._credentials is not None

        full_query = query
        if opts.scope:  # channel scope, e.g. "#general"
            full_query = f"in:{opts.scope} {full_query}"
        if opts.author:
            full_query = f"from:{opts.author} {full_query}"

        response = await self._client.get(
            "/search.messages",
            params={
                "query": full_query,
                "count": min(opts.limit, 100),
                "sort": "timestamp",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack search.messages failed: {payload.get('error')}")

        matches = payload.get("messages", {}).get("matches", [])
        return [self._message_to_result(m) for m in matches]

    async def lookup(self, term: str) -> Result | None:
        """`term` is a channel name (with or without a leading `#`) -
        resolves to that channel's topic/purpose as a lightweight
        "definition" lookup, bypassing full-text message search."""
        if self._client is None:
            await self.initialize()
        assert self._client is not None and self._credentials is not None

        channel_name = term.lstrip("#")
        # conversations.list is paginated; a workspace with >1000 channels
        # would silently drop the target if we only read the first page. Walk
        # the cursor until we find the channel or run out (capped so a huge
        # workspace can't spin indefinitely).
        cursor = ""
        for _ in range(_MAX_CHANNEL_LIST_PAGES):
            params: dict[str, Any] = {
                "limit": 1000,
                "types": "public_channel,private_channel",
            }
            if cursor:
                params["cursor"] = cursor
            response = await self._client.get("/conversations.list", params=params)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                return None

            for channel in payload.get("channels", []):
                if channel.get("name") == channel_name:
                    content = channel.get("purpose", {}).get("value") or channel.get(
                        "topic", {}
                    ).get("value", "")
                    return Result(
                        source="slack",
                        id=channel["id"],
                        title=f"#{channel['name']}",
                        content=content,
                        permalink=f"https://app.slack.com/client/_/{channel['id']}",
                        metadata={"channel": channel["name"]},
                    )

            cursor = payload.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        return None

    @staticmethod
    def _message_to_result(match: dict[str, Any]) -> Result:
        channel = match.get("channel", {})
        ts = match.get("ts", "0")
        return Result(
            source="slack",
            id=f"{channel.get('id', 'unknown')}.{ts}",
            title=f"#{channel.get('name', 'unknown')} - {match.get('username', 'unknown')}",
            content=match.get("text", ""),
            timestamp=datetime.fromtimestamp(float(ts)) if ts else None,
            permalink=match.get("permalink"),
            metadata={"channel": channel.get("name")},
        )
