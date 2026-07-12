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
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlparse

from columbo_py.config import SETTINGS

DEFAULT_DOMAINS_PATH = SETTINGS.paths.domains_path


class ErrAborted(Exception):
    """User aborted a domain-gate prompt; the orchestrator ends the run cleanly."""


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
