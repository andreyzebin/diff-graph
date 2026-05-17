"""Unified template engine — one Jinja Environment for all
framework templates plus a backward-compat path for legacy
`{x}` placeholder prompts.

Design:
- One `jinja2.Environment` with FileSystemLoader pointing at the
  framework template directories (budget_stats, reflect_response).
  Add more dirs here as needed.
- `render(text, ctx)` — auto-detect: if `text` contains Jinja
  markers (`{{` or `{%`), render via Jinja; otherwise route to
  the legacy `orchestra.prompts.interpolate` regex substitution.
  Both engines see the same RunContext (via `to_kwargs()`), so
  the same variable names work in either syntax.
- `render_named(name, ctx)` — load a framework template by short
  name from the configured directories and render via Jinja.
  Framework `.md` files are assumed to use modern syntax.
- Jinja globals expose framework helpers (e.g. `bar(part, whole)`
  for progress bars) so templates can call them directly without
  the agent pre-computing values.

Why auto-detect (and not migrate everything to Jinja today):
- Dozens of existing user prompts use `{pr_title}` style
  substitution. A flag-day migration would touch every prompt
  file + every scenario yaml. Auto-detect lets each prompt
  opt into Jinja when it wants loops / conditionals, with zero
  pressure on prompts that just want simple substitution.
- The detection is cheap (regex on the text) and the engines
  share the same data — no behavioural drift.
"""
from __future__ import annotations

import re
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
        undefined=jinja2.ChainableUndefined,   # missing var → "" not exception
    )
    # Helpers exposed to every template (no per-call wiring needed).
    from .budget_stats import _bar
    env.globals["bar"] = _bar
    return env


_env = _build_env()


_JINJA_MARKERS = re.compile(r"\{\{|\{%")


def render(template_text: str, ctx: "RunContext") -> str:
    """Render a template string against a RunContext.

    Auto-detects engine: text with `{{` or `{%` goes through
    Jinja; everything else uses the legacy `{x}` regex
    substitution. Both paths see the same flat variable
    namespace via `ctx.to_kwargs()` — same names, same values."""
    if not template_text:
        return ""
    kwargs = ctx.to_kwargs()
    if _JINJA_MARKERS.search(template_text):
        return _env.from_string(template_text).render(**kwargs)
    from .prompts import interpolate
    return interpolate(template_text, **kwargs)


def render_named(name: str, ctx: "RunContext") -> str:
    """Load a template by short name from the framework
    directories and render via Jinja against a RunContext.
    Framework `.md` files use Jinja syntax; user prompts use
    `render()` which auto-detects."""
    template = _env.get_template(f"{name}.md")
    return template.render(**ctx.to_kwargs())


def render_named_kwargs(name: str, **kwargs) -> str:
    """Load a template by short name and render with a plain
    kwargs dict — no RunContext wrapper, no framework-helper
    overrides. Use for internal framework templates that build
    their own narrow variable set (e.g. `budget_stats` table
    cells) and don't want the RunContext-level helpers like
    `{{ skills }}` polluting the namespace."""
    template = _env.get_template(f"{name}.md")
    return template.render(**kwargs)
