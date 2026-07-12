"""Google Drive source: real Drive API v3 file search via httpx, using an
OAuth access token from the standard installed-app consent flow (see
auth.py) - a user-owned credential, not a service account.

`files.list` only returns metadata, not document content - Drive doesn't
expose full text through that endpoint. When `opts.augment` is set and the
file is a native Google Doc/Sheet/Slide, we additionally call `files.export`
to pull plain text; anything else (PDFs, images, uploaded Office docs) stays
metadata-only here and is a candidate for a `scrape` action instead.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from columbo_py.config import SETTINGS
from columbo_py.engine.search.result import Options, Result
from columbo_py.engine.search.source import Capabilities
from columbo_py.sources.drive.auth import DEFAULT_TOKEN_PATH, get_access_token
from columbo_py.sources.guidance import load_guidance

DRIVE_API_BASE = SETTINGS.drive.api_base
KIND_DESCRIPTION = load_guidance(__file__)

_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


class DriveSource:
    def __init__(
        self,
        client_secrets_path: Path | None = None,
        token_path: Path = DEFAULT_TOKEN_PATH,
    ) -> None:
        env_secrets = os.environ.get("GOOGLE_CLIENT_SECRETS_PATH")
        self._client_secrets_path = client_secrets_path or (
            Path(env_secrets) if env_secrets else None
        )
        self._token_path = token_path
        self._client: httpx.AsyncClient | None = None

    def name(self) -> str:
        return "drive"

    def kind(self) -> str:
        return KIND_DESCRIPTION

    def capabilities(self) -> Capabilities:
        return Capabilities(
            filters=(), supports_days=True, supports_author=False, supports_scope=True, max_limit=100
        )

    async def initialize(self) -> None:
        if self._client is not None:
            return
        if self._client_secrets_path is None:
            raise RuntimeError(
                "Google Drive not configured: set GOOGLE_CLIENT_SECRETS_PATH "
                "to an OAuth client secrets JSON downloaded from the Google "
                "Cloud console (APIs & Services > Credentials > OAuth "
                "client ID > Desktop app)."
            )
        token = await get_access_token(self._client_secrets_path, self._token_path)
        self._client = httpx.AsyncClient(
            base_url=DRIVE_API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=SETTINGS.http.default_timeout_s,
        )

    def _build_query(self, query: str, opts: Options) -> str:
        escaped = query.replace("'", "\\'")
        terms = [f"fullText contains '{escaped}'", "trashed = false"]
        if opts.scope:
            terms.append(f"'{opts.scope}' in parents")
        if opts.days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=opts.days)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            terms.append(f"modifiedTime >= '{cutoff}'")
        return " and ".join(terms)

    async def search(self, query: str, opts: Options) -> list[Result]:
        if self._client is None:
            await self.initialize()
        assert self._client is not None

        response = await self._client.get(
            "/files",
            params={
                "q": self._build_query(query, opts),
                "pageSize": min(opts.limit, 100),
                "fields": "files(id,name,mimeType,webViewLink,modifiedTime,owners)",
            },
        )
        response.raise_for_status()
        files = response.json().get("files", [])
        results = []
        for f in files:
            content = await self._export_text(f) if opts.augment else None
            results.append(self._file_to_result(f, content))
        return results

    async def lookup(self, term: str) -> Result | None:
        """`term` is a Drive file ID."""
        if self._client is None:
            await self.initialize()
        assert self._client is not None

        response = await self._client.get(
            f"/files/{term}",
            params={"fields": "id,name,mimeType,webViewLink,modifiedTime,owners"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        file = response.json()
        content = await self._export_text(file)
        return self._file_to_result(file, content)

    async def _export_text(self, file: dict[str, Any]) -> str | None:
        assert self._client is not None
        export_mime = _EXPORT_MIME_TYPES.get(file.get("mimeType", ""))
        if export_mime is None:
            return None
        response = await self._client.get(
            f"/files/{file['id']}/export", params={"mimeType": export_mime}
        )
        if response.status_code != 200:
            return None
        return response.text

    def _file_to_result(self, file: dict[str, Any], full_text: str | None) -> Result:
        modified = file.get("modifiedTime")
        owners = ", ".join(o.get("displayName", "") for o in file.get("owners", []))
        content = full_text or f"({file.get('mimeType', 'unknown type')}, owned by {owners or 'unknown'})"
        return Result(
            source="drive",
            id=file["id"],
            title=file.get("name", ""),
            content=content,
            timestamp=(
                datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None
            ),
            permalink=file.get("webViewLink"),
            metadata={"mime_type": file.get("mimeType")},
        )
