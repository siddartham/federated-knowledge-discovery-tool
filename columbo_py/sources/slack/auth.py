"""Slack authentication - two paths to a user-scoped token:

1. Native OAuth (`get_user_token`): the standard Slack OAuth 2.0 user-token
   flow. Register a Slack app once, and this runs the consent screen in a local
   browser tab, exchanges the code for an `xoxp-` user token, and caches it to
   disk - directly analogous to the Google Drive OAuth flow. No `d` cookie is
   needed; the `xoxp-` token authenticates on its own. This is the preferred,
   most robust path but requires a registered app (and often workspace-admin
   approval of the `search:read` scope).

2. Browser interception (`intercept_slack_token`): the zero-provisioning
   fallback. Reads the `xoxc-` token out of an already-logged-in Slack web tab
   plus the `d` session cookie the browser sends automatically. Needs no app
   registration, but is inherently more fragile (depends on Slack's web client
   internals) - kept as a fallback for when registering an app isn't an option.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

from columbo_py.config import SETTINGS
from columbo_py.infra.browser.session import BrowserSession

# Endpoints/scope/paths from config/defaults.toml ([sources.slack]);
# COLUMBO_SLACK_TOKEN_PATH overrides the cached-token location.
SLACK_CLIENT_URL = SETTINGS.slack.client_url

# Native OAuth user-token flow.
SLACK_AUTHORIZE_URL = SETTINGS.slack.authorize_url
SLACK_TOKEN_URL = SETTINGS.slack.token_url
SLACK_USER_SCOPES = SETTINGS.slack.user_scopes
DEFAULT_SLACK_TOKEN_PATH = SETTINGS.slack.token_path

# Slack's web client stores the per-workspace xoxc token in a JS global
# (`localConfig_v2.teams`) rather than a cookie - this has shifted with past
# Slack web client rewrites and may need updating if Slack changes it again.
_EXTRACT_TOKEN_JS = """
() => {
    const configs = window.localConfig_v2 && window.localConfig_v2.teams;
    if (!configs) return null;
    const team = Object.values(configs)[0];
    return team ? team.token : null;
}
"""
_EXTRACT_TEAM_ID_JS = "() => window.boot_data && window.boot_data.team_id"


@dataclass(slots=True)
class SlackCredentials:
    token: str  # xoxp-... (native OAuth) or xoxc-... (browser) user token
    d_cookie: str | None  # `d` session cookie - required for xoxc, unused for xoxp
    team_id: str | None


async def intercept_slack_token(session: BrowserSession) -> SlackCredentials:
    """Opens a tab against the already-authenticated Slack web client, reads
    the xoxc token + team id out of the page's JS globals, then reads the
    `d` cookie out of the browser context. Raises RuntimeError if the user
    isn't logged into Slack in this browser profile."""
    page = await session.new_tab()
    try:
        await page.goto(SLACK_CLIENT_URL, wait_until="domcontentloaded")
        token = await page.evaluate(_EXTRACT_TOKEN_JS)
        team_id = await page.evaluate(_EXTRACT_TEAM_ID_JS)
        if not token or not token.startswith("xoxc-"):
            raise RuntimeError(
                "could not find a valid xoxc- token - log into Slack in this "
                "browser profile (see BrowserSession.user_data_dir) and retry"
            )
    finally:
        await page.close()

    cookies = await session.context.cookies(SLACK_CLIENT_URL)
    d_cookie = next((c["value"] for c in cookies if c["name"] == "d"), None)
    if not d_cookie:
        raise RuntimeError("could not find the Slack `d` session cookie")

    return SlackCredentials(token=token, d_cookie=d_cookie, team_id=team_id)


async def get_user_token(
    client_id: str,
    client_secret: str,
    token_path: Path = DEFAULT_SLACK_TOKEN_PATH,
) -> str:
    """Return a Slack `xoxp-` user token via the native OAuth flow, using a
    cached token if one is on disk. The consent flow opens a local browser tab
    and blocks on a localhost redirect, so - like the Drive flow - it runs in a
    worker thread; the common case (a valid cached token) returns instantly."""
    if token_path.exists():
        cached = json.loads(token_path.read_text()).get("access_token")
        if cached:
            return str(cached)
    return await asyncio.to_thread(_run_oauth_flow, client_id, client_secret, token_path)


def _run_oauth_flow(client_id: str, client_secret: str, token_path: Path) -> str:
    """Synchronous, one-time OAuth consent: spin an ephemeral localhost server
    to catch Slack's redirect, open the authorize URL, exchange the returned
    code for a user token, and cache it. Slack user tokens don't expire unless
    token rotation is enabled on the app, so there's no refresh step here."""
    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            query = urllib.parse.urlparse(self.path).query
            code = urllib.parse.parse_qs(query).get("code", [""])[0]
            if code:
                captured["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Slack authorization complete - you can close this tab.</body></html>")

        def log_message(self, *_: object) -> None:  # silence request logging
            return

    server = http.server.HTTPServer(("localhost", 0), _Handler)
    try:
        redirect_uri = f"http://localhost:{server.server_address[1]}"
        authorize_url = SLACK_AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {"client_id": client_id, "user_scope": SLACK_USER_SCOPES, "redirect_uri": redirect_uri}
        )
        webbrowser.open(authorize_url)
        server.handle_request()  # blocks until Slack redirects back once
    finally:
        server.server_close()

    code = captured.get("code")
    if not code:
        raise RuntimeError("Slack OAuth: no authorization code received from the redirect")

    with httpx.Client(timeout=SETTINGS.http.default_timeout_s) as client:
        response = client.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Slack OAuth token exchange failed: {payload.get('error')}")
    # The user token lives under authed_user, not the top-level (bot) token.
    token = payload.get("authed_user", {}).get("access_token")
    if not token:
        raise RuntimeError("Slack OAuth: response contained no authed_user.access_token")

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps({"access_token": token, "team_id": payload.get("team", {}).get("id")})
    )
    return str(token)
