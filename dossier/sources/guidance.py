"""Loads a source's LLM-facing query guidance from a co-located markdown file.

Each source ships a GUIDANCE.md (GitHub ships two: one per search mode) next to
its module. The full guidance is injected verbatim into the planner prompt via
`Registry.sources_prompt_block`, so the model knows each source's query language,
qualifiers, and strengths - not just a one-line description. Keeping the guides
as markdown files (rather than long string literals in code) makes them easy to
review and edit without touching Python.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def _read(path_str: str) -> str:
    return Path(path_str).read_text(encoding="utf-8").strip()


def load_guidance(module_file: str, filename: str = "GUIDANCE.md") -> str:
    """Reads `filename` from the directory of `module_file` (pass `__file__`).
    Cached, so repeated plan calls don't re-read from disk."""
    return _read(str(Path(module_file).parent / filename))
