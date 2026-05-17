"""Unified template engine — one Jinja Environment for every
template surface in the framework.

Design:
- One `jinja2.Environment` with FileSystemLoader pointing at the
  framework template directories (budget_stats, reflect_response).
- `render(text, ctx)` — render an inline template string against
  a RunContext. Always Jinja.
- `render_named(name, ctx)` — load a framework template by short
  name from the configured directories and render against a
  RunContext.
- `render_named_kwargs(name, **kw)` — load by name and render
  against a plain kwargs dict. Use for internal framework
  templates that build their own narrow variable set (e.g.
  `budget_stats` table cells) and don't want the RunContext
  helpers like `{{ skills }}` polluting the namespace.
- Jinja globals expose framework helpers (e.g. `bar(part, whole)`
  for progress bars) so templates can call them directly without
  the agent pre-computing values.

`ChainableUndefined` is the default — missing variables render
as empty strings rather than raising. Pragmatic for opt-in
placeholders like `{{ skills }}` that may or may not be
populated for a given run.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import jinja2


if TYPE_CHECKING:
    from .runcontext import RunContext


_BASE = Path(__file__).parent / "templates"
_TEMPLATES_DIRS = [
    _BASE / "budget_stats",
    _BASE / "reflect_response",
]


def _build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader([str(d) for d in _TEMPLATES_DIRS]),
        autoescape=False,            # rendering plain text, not HTML
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.ChainableUndefined,
    )
    from .budget_stats import _bar
    env.globals["bar"] = _bar
    return env


_env = _build_env()


def render(template_text: str, ctx: "RunContext") -> str:
    """Render a template string against a RunContext. Always Jinja."""
    if not template_text:
        return ""
    return _env.from_string(template_text).render(**ctx.to_kwargs())


def render_named(name: str, ctx: "RunContext") -> str:
    """Load a template by short name from the framework
    directories and render against a RunContext."""
    return _env.get_template(f"{name}.md").render(**ctx.to_kwargs())


def render_named_kwargs(name: str, **kwargs) -> str:
    """Load a template by short name and render with a plain
    kwargs dict. Use for internal framework templates that need
    a narrow variable set free of RunContext's framework
    helpers."""
    return _env.get_template(f"{name}.md").render(**kwargs)
