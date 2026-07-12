"""Domain gate: the last check before a URL is scraped.

A scrape reaches out from the user's own browser session to an arbitrary host
the LLM chose - so before any fetch we vet the host against a persisted
allow/deny list:

- host on the allowlist  -> allowed silently
- host on the denylist   -> dropped silently
- unknown host           -> a one-time interactive y/N/abort prompt; the
                            answer is written to disk so the same domain is
                            never asked again (this run or any future run)

Aborting the prompt raises `ErrAborted`, which the orchestrator catches to end
the current run cleanly (synthesizing from whatever was gathered) rather than
crashing. Subdomains match their registrable parent, so allowing "github.com"
also allows "gist.github.com".
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlparse

from dossier.config import SETTINGS

DEFAULT_DOMAINS_PATH = SETTINGS.paths.domains_path

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Hostnames that name the local machine or a cloud metadata endpoint. Blocked
# outright regardless of the allow/deny list.
_BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})


class ErrAborted(Exception):
    """User aborted a domain-gate prompt; the orchestrator ends the run cleanly."""


def ssrf_reason(url: str) -> str | None:
    """SSRF guard: return why a URL must never be fetched, or None if it's a
    normal public web URL. Blocks non-http(s) schemes (file://, gopher://, ...)
    and any host that is localhost, a cloud-metadata name, or a literal private/
    loopback/link-local/reserved IP - so a URL planted in evidence can't make
    the scraper reach into the intranet or the 169.254.169.254 metadata service.
    This check precedes (and cannot be overridden by) the allow/deny list.

    Static by design - it does NOT resolve DNS, so a public hostname that
    resolves to a private IP (DNS rebinding) is out of scope here; that belongs
    at the socket layer of whatever HTTP client performs the fetch."""
    parts = urlparse(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"scheme {scheme!r} is not http/https"
    host = (parts.hostname or "").lower()
    if not host:
        return "url has no host"
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return f"host {host!r} is local/metadata"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # a normal hostname; the allow/deny list decides
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return f"host {host!r} is a non-public IP"
    return None


def _host_matches(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _default_prompt(host: str) -> str:
    return input(f"Scrape from unknown domain '{host}'? [y = allow / N = deny / a = abort] ")


class DomainGate:
    def __init__(
        self,
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
        *,
        store_path: Path | None = None,
        prompt: Callable[[str], str] | None = None,
    ) -> None:
        self._allow = {d.lower() for d in allow}
        self._deny = {d.lower() for d in deny}
        self._store_path = store_path
        self._prompt = prompt or _default_prompt
        # Serializes interactive prompts so concurrent scrapes in one fan-out
        # don't ask overlapping questions on the same terminal.
        self._lock = asyncio.Lock()

    @classmethod
    def load(
        cls,
        store_path: Path | None = DEFAULT_DOMAINS_PATH,
        *,
        allow_seed: Iterable[str] = (),
        prompt: Callable[[str], str] | None = None,
    ) -> DomainGate:
        allow = {d.lower() for d in allow_seed}
        deny: set[str] = set()
        if store_path is not None and store_path.exists():
            data = json.loads(store_path.read_text(encoding="utf-8"))
            allow |= {d.lower() for d in data.get("allow", [])}
            deny |= {d.lower() for d in data.get("deny", [])}
        return cls(allow, deny, store_path=store_path, prompt=prompt)

    def _persist(self) -> None:
        if self._store_path is None:
            return
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_text(
            json.dumps({"allow": sorted(self._allow), "deny": sorted(self._deny)}, indent=2),
            encoding="utf-8",
        )

    async def allows(self, url: str) -> bool:
        """True if `url`'s host may be scraped. Prompts (once) for unknown
        hosts and persists the answer. Raises ErrAborted if the user aborts."""
        # SSRF guard runs first and is non-overridable: a private/loopback/
        # metadata host or non-http scheme is dropped even if it were somehow
        # on the allowlist.
        if ssrf_reason(url) is not None:
            return False
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        if _host_matches(host, self._allow):
            return True
        if _host_matches(host, self._deny):
            return False

        async with self._lock:
            # Re-check under the lock: a concurrent scrape may have resolved
            # this same host while we were waiting.
            if _host_matches(host, self._allow):
                return True
            if _host_matches(host, self._deny):
                return False
            try:
                answer = (await asyncio.to_thread(self._prompt, host)).strip().lower()
            except (EOFError, KeyboardInterrupt) as exc:
                raise ErrAborted(host) from exc
            if answer in ("a", "abort"):
                raise ErrAborted(host)
            if answer in ("y", "yes"):
                self._allow.add(host)
                self._persist()
                return True
            self._deny.add(host)
            self._persist()
            return False
