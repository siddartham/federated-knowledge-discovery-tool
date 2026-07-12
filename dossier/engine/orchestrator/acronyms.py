"""Detect candidate internal acronyms/abbreviations in free text so they can
be resolved against the company dictionary and fed back into the plan prompt.

Heuristic: runs of 2-8 uppercase letters/digits starting with a letter
(SSO, TTL, RBAC, MFA, ENG2). A small stoplist drops ubiquitous technical
acronyms the dictionary is unlikely to define, to avoid pointless lookups.
"""

from __future__ import annotations

import re

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")

# Common all-caps tokens (English words + universal tech terms) that aren't
# worth a dictionary round-trip.
_STOPWORDS = frozenset(
    {
        "A", "I", "AN", "AND", "OR", "THE", "TO", "OF", "IN", "ON", "IS", "IT", "BE", "AS",
        "AT", "BY", "IF", "NO", "OK", "SO", "US", "UK", "AM", "PM", "TV", "OK",
        "API", "URL", "URI", "HTTP", "HTTPS", "JSON", "YAML", "HTML", "CSS", "SQL", "ID",
        "UUID", "CLI", "SDK", "OS", "UI", "UX", "PR", "CI", "CD", "AWS", "GCP",
        "TODO", "FIXME", "NOTE", "FAQ", "PDF", "CSV", "PNG", "JPG",
    }
)


def extract_acronyms(text: str) -> set[str]:
    """Candidate acronyms in `text`, minus the stoplist."""
    return {m.group() for m in _ACRONYM_RE.finditer(text)} - _STOPWORDS
