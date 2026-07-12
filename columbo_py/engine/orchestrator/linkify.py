"""Post-process the synthesis answer to turn inline `[source:id]` citation
tokens into clickable markdown links, and append a Sources footer.

Done deterministically here rather than in the prompt: the model emits clean
`[source:id]` tokens, and we rewrite them against the permalink map we already
have from the selected evidence. That means the URLs are always correct (never
hallucinated), only tokens we actually have a permalink for get linked, and the
output stays a pure function of (answer, evidence) - so it remains cache-stable.
"""

from __future__ import annotations

import re

from columbo_py.engine.orchestrator.models import Citation

# `[source:id]` where source has no ':' and neither part has brackets. The
# trailing `(?!\()` skips any token the model already turned into a markdown
# link (`[x:y](...)`), so we never double-link.
_CITATION_RE = re.compile(r"\[([^\[\]:]+):([^\[\]]+)\](?!\()")

PermalinkMap = dict[tuple[str, str], str]


def linkify_citations(answer: str, permalinks: PermalinkMap) -> str:
    """Rewrite inline `[source:id]` tokens into `[source:id](permalink)` for
    every token whose (source, id) has a known permalink. Tokens without one
    (or already linked) are left exactly as the model wrote them."""

    def repl(match: re.Match[str]) -> str:
        source, cid = match.group(1).strip(), match.group(2).strip()
        url = permalinks.get((source, cid))
        if not url:
            return match.group(0)
        return f"[{source}:{cid}]({url})"

    return _CITATION_RE.sub(repl, answer)


def render_sources_footer(citations: list[Citation], permalinks: PermalinkMap) -> str:
    """Build a `**Sources**` bullet list from the model's citations, one link
    per unique (source, id) that has a permalink. Prefers the authoritative
    permalink from the evidence map, falling back to whatever the model
    returned. Returns "" when nothing is linkable."""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for c in citations:
        key = (c.source, c.id)
        if key in seen:
            continue
        url = permalinks.get(key) or c.permalink
        if not url:
            continue
        seen.add(key)
        lines.append(f"- [{c.source}:{c.id}]({url})")
    if not lines:
        return ""
    return "\n\n**Sources**\n" + "\n".join(lines)
