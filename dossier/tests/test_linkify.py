from __future__ import annotations

from dossier.engine.orchestrator.linkify import (
    linkify_citations,
    render_sources_footer,
)
from dossier.engine.orchestrator.models import Citation


def test_linkify_rewrites_known_tokens_only() -> None:
    permalinks = {
        ("slack", "C123.456"): "https://acme.slack.com/archives/C123/p456",
        ("github", "42"): "https://github.com/acme/repo/pull/42",
    }
    answer = "We ship via [github:42], discussed in [slack:C123.456] and [jira:PROJ-9]."
    out = linkify_citations(answer, permalinks)

    assert "[github:42](https://github.com/acme/repo/pull/42)" in out
    assert "[slack:C123.456](https://acme.slack.com/archives/C123/p456)" in out
    # Unknown permalink → token left untouched.
    assert "[jira:PROJ-9]" in out
    assert "[jira:PROJ-9](" not in out


def test_linkify_does_not_double_link_already_linked_tokens() -> None:
    permalinks = {("github", "42"): "https://github.com/acme/repo/pull/42"}
    # Model already emitted a link target - leave it alone (no (url)(url)).
    answer = "See [github:42](https://elsewhere.example/42)."
    out = linkify_citations(answer, permalinks)
    assert out == answer


def test_render_sources_footer_dedupes_and_skips_urlless() -> None:
    permalinks = {("github", "42"): "https://github.com/acme/repo/pull/42"}
    citations = [
        Citation(source="github", id="42"),  # url from evidence map
        Citation(source="github", id="42"),  # duplicate → collapsed
        Citation(source="confluence", id="9", permalink="https://wiki/x"),  # url from model
        Citation(source="jira", id="7"),  # no url anywhere → skipped
    ]
    footer = render_sources_footer(citations, permalinks)

    assert footer.startswith("\n\n**Sources**\n")
    assert footer.count("- [") == 2  # dedup + skip
    assert "- [github:42](https://github.com/acme/repo/pull/42)" in footer
    assert "- [confluence:9](https://wiki/x)" in footer
    assert "jira:7" not in footer


def test_render_sources_footer_empty_when_nothing_linkable() -> None:
    citations = [Citation(source="jira", id="7")]
    assert render_sources_footer(citations, {}) == ""


def test_evidence_map_permalink_beats_model_supplied_one() -> None:
    # The evidence map is authoritative: if the model returns a stale/wrong
    # permalink for a citation, the footer uses the evidence one.
    permalinks = {("github", "42"): "https://github.com/acme/repo/pull/42"}
    citations = [Citation(source="github", id="42", permalink="https://wrong.example")]
    footer = render_sources_footer(citations, permalinks)
    assert "https://github.com/acme/repo/pull/42" in footer
    assert "wrong.example" not in footer
