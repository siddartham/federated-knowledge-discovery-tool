"""Defensive JSON extraction for LLM responses. Chat models don't reliably
return bare JSON - they wrap it in markdown code fences, prepend chatty
preamble, or occasionally truncate output before the closing brace. This
gives every call site one place to recover from that instead of duct-taping
try/except around `json.loads` at each one.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction, tried in order:

    1. The raw text, as-is.
    2. The text with markdown code fences stripped.
    3. Brace/bracket-scanning for the first complete top-level JSON value
       anywhere in the text (handles chatty preamble like "Sure, here you
       go:" before the JSON starts).

    Raises the last `json.JSONDecodeError` if nothing parses.
    """
    stripped = text.strip()
    candidates = [text, _FENCE_RE.sub("", text).strip()]

    if stripped:
        open_char, close_char = ("[", "]") if stripped.lstrip().startswith("[") else ("{", "}")
        start = stripped.find(open_char)
        if start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i, ch in enumerate(stripped[start:], start=start):
                # Track string state so braces/brackets *inside* JSON string
                # values (e.g. a "thinking" field discussing `{code}`) don't
                # move the depth counter and truncate the value early.
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == open_char:
                    depth += 1
                elif ch == close_char:
                    depth -= 1
                    if depth == 0:
                        candidates.append(stripped[start : i + 1])
                        break

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
