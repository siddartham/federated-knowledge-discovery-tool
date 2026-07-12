"""Jinja2-based prompt renderer - the closest Python analog to Go's
text/template. Each template file defines two named blocks, `system` and
`user`, rendered independently so callers get a (system_prompt, user_prompt)
pair to hand to the LLM client without needing a second template per stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def render_prompt(template_name: str, **context: Any) -> tuple[str, str]:
    """Renders a template's `system` and `user` blocks with the given context.
    Returns (system_prompt, user_prompt), both stripped of surrounding
    whitespace so they're stable cache keys across trivial formatting diffs.
    """
    template = _env.get_template(template_name)
    ctx = template.new_context(context)
    rendered = {name: "".join(block(ctx)).strip() for name, block in template.blocks.items()}
    return rendered.get("system", ""), rendered.get("user", "")
