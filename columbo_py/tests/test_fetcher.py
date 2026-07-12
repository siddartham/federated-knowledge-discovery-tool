from __future__ import annotations

from columbo_py.infra.browser.fetcher import _html_to_text


def test_html_to_text_strips_tags_scripts_and_styles() -> None:
    html = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var x = {a: 1};</script></head>"
        "<body><h1>Title</h1><p>Hello  world</p></body></html>"
    )
    text = _html_to_text(html)
    assert "Title" in text
    assert "Hello world" in text
    # Script/style bodies and tag markup must be gone.
    assert "color:red" not in text
    assert "var x" not in text
    assert "<" not in text and ">" not in text
