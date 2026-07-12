from __future__ import annotations

import json
from pathlib import Path

import pytest

from columbo_py.infra.domaingate import DomainGate, ErrAborted


async def test_allowlist_and_denylist_do_not_prompt() -> None:
    def _boom(host: str) -> str:  # must never be called
        raise AssertionError(f"prompted for known host {host!r}")

    gate = DomainGate(allow=["github.com"], deny=["evil.example"], prompt=_boom)
    assert await gate.allows("https://gist.github.com/x")  # subdomain matches parent
    assert not await gate.allows("https://evil.example/x")


async def test_unknown_host_prompts_once_and_persists(tmp_path: Path) -> None:
    store = tmp_path / "domains.json"
    calls: list[str] = []

    def _prompt(host: str) -> str:
        calls.append(host)
        return "y"

    gate = DomainGate(store_path=store, prompt=_prompt)
    assert await gate.allows("https://docs.example.com/a")
    # Second URL on the same host must not prompt again.
    assert await gate.allows("https://docs.example.com/b")
    assert calls == ["docs.example.com"]

    # Answer persisted to disk, and a freshly loaded gate honors it.
    data = json.loads(store.read_text())
    assert "docs.example.com" in data["allow"]
    reloaded = DomainGate.load(store, prompt=lambda h: (_ for _ in ()).throw(AssertionError()))
    assert await reloaded.allows("https://docs.example.com/c")


async def test_deny_answer_persists(tmp_path: Path) -> None:
    store = tmp_path / "domains.json"
    gate = DomainGate(store_path=store, prompt=lambda h: "n")
    assert not await gate.allows("https://spam.example/x")
    assert "spam.example" in json.loads(store.read_text())["deny"]


async def test_abort_raises_err_aborted() -> None:
    gate = DomainGate(prompt=lambda h: "a")
    with pytest.raises(ErrAborted):
        await gate.allows("https://unknown.example/x")


async def test_eof_on_prompt_aborts() -> None:
    def _eof(host: str) -> str:
        raise EOFError

    gate = DomainGate(prompt=_eof)
    with pytest.raises(ErrAborted):
        await gate.allows("https://unknown.example/x")
