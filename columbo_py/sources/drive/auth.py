"""Google Drive OAuth: standard installed-app consent flow via
google-auth-oauthlib, with the resulting refresh token cached to disk so the
user only completes the browser consent screen once. Uses the official
google-auth libraries for credential storage/refresh (well-tested, handles
token expiry edge cases) - the Drive API calls themselves go through httpx
elsewhere in this package, for consistency and to stay fully async.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from columbo_py.config import SETTINGS

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# COLUMBO_DRIVE_TOKEN_PATH overrides where the cached refresh token lives.
DEFAULT_TOKEN_PATH = SETTINGS.drive.token_path


def _load_or_run_oauth_flow(client_secrets_path: Path, token_path: Path) -> Credentials:
    """Loads a cached token if present (refreshing if expired), otherwise
    runs the interactive OAuth consent flow in a local browser tab. This is
    a synchronous, one-time setup step - safe to run outside the event loop,
    which is why `get_access_token` below offloads it to a thread."""
    creds: Credentials | None = None
    if token_path.exists():
        # google-auth ships no py.typed marker, so these calls are untyped
        # from mypy's perspective even though they're perfectly well-typed
        # at runtime.
        creds = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
            json.loads(token_path.read_text()), SCOPES
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())  # type: ignore[no-untyped-call]

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    return creds


async def get_access_token(client_secrets_path: Path, token_path: Path = DEFAULT_TOKEN_PATH) -> str:
    """Async-friendly wrapper: the OAuth flow itself is synchronous and may
    open a local browser tab, so it runs in a worker thread. The common case
    (a valid cached token) returns almost instantly."""
    creds = await asyncio.to_thread(_load_or_run_oauth_flow, client_secrets_path, token_path)
    assert creds.token is not None
    return cast(str, creds.token)
