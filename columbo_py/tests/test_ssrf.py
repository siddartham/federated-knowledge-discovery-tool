"""Tests for the SSRF guard in the domain gate: private/loopback/metadata hosts
and non-http schemes are refused even when the host is on the allowlist."""

from __future__ import annotations

import pytest

from columbo_py.infra.domaingate import DomainGate


def _gate() -> DomainGate:
    # No store, no prompt: a public host not covered here would try to prompt,
    # so tests only exercise blocked URLs + explicitly-allowlisted public hosts.
    return DomainGate(allow=["evil.example.com"])


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost:8080/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.4.4/",
        "http://[::1]/x",
        "http://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/passwd",
        "gopher://internal/",
    ],
)
async def test_dangerous_urls_are_blocked(url: str) -> None:
    assert await _gate().allows(url) is False


async def test_public_allowlisted_host_still_passes() -> None:
    gate = _gate()
    assert await gate.allows("https://evil.example.com/page") is True
    assert await gate.allows("https://sub.evil.example.com/page") is True  # subdomain match


async def test_ssrf_block_is_not_overridable_by_allowlist() -> None:
    # Even if a loopback host were somehow allowlisted, the SSRF guard wins.
    gate = DomainGate(allow=["127.0.0.1", "localhost"])
    assert await gate.allows("http://127.0.0.1/") is False
    assert await gate.allows("http://localhost/") is False
