"""Secret redaction shared by the input filter (keep secrets out of the run
log) and the output guard (keep a secret that surfaced in evidence out of the
answer). Deterministic and dependency-free - a curated set of high-signal
patterns chosen to catch real credentials with few false positives, not a
general PII scrubber. Matches are replaced with a visible «redacted» marker so
the redaction is auditable rather than silent.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# (kind, pattern). Ordered: multi-line key blocks first so their body isn't
# partially eaten by a narrower rule.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN(?:[A-Z ]+)?PRIVATE KEY-----[\s\S]*?-----END(?:[A-Z ]+)?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[0-9A-Za-z._\-]{16,}")),
    # key=value / key: value assignments for the usual secret-bearing names.
    ("assigned_secret", re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b"
        r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{8,})\3"
    )),
)


def _replacement(kind: str) -> Callable[[re.Match[str]], str]:
    if kind == "assigned_secret":
        # Keep the key name + separator so the log still reads `api_key=«redacted»`.
        return lambda m: f"{m.group(1)}{m.group(2)}«redacted»"
    return lambda m: f"«redacted:{kind}»"


def redact_secrets(text: str) -> tuple[str, int]:
    """Return (redacted_text, count). count is how many secrets were replaced."""
    if not text:
        return text, 0
    total = 0
    for kind, pattern in _PATTERNS:
        text, n = pattern.subn(_replacement(kind), text)
        total += n
    return text, total
