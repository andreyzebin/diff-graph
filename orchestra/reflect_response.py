"""Reflect tool's tool-result string — composed from internal render
APIs and a template file.

Architecture:
- Internal APIs (callable, mockable like fake_bitbucket) live as
  pure functions in their own modules: `format_budget_stats`,
  `format_time_info`, … Each takes the agent's state and returns a
  rendered string block.
- Templates live in `orchestra/templates/reflect_response/<name>.md`.
  They reference internal APIs by placeholder name; `render(...)`
  resolves each.
- Agents opt into a non-default template via the
  `reflect_response_template:` frontmatter field
  (AgentConfig.reflect_response_template). Default `"default"`
  returns just "Reflection noted." — unchanged from the original
  reflect handler.

Why this shape:
- `reflect` is the agent's natural planning moment. Carrying a
  state snapshot in its response gives continuous awareness without
  the agent having to remember to call a query tool (TODO §12 →
  §13 design follow-up, 2026-05-16).
- Templates as files mean wording / structure can be tuned without
  code changes — same pattern as `budget_stats.md`.
- Templates as toggle (per-prompt) keep backward-compat — existing
  prompts get the old plain "Reflection noted." default.

Adding a new internal API:
  1. Write `format_X(state, ...) -> str` in its own module.
  2. Add `{X}` placeholder support in `render()` below.
  3. Reference `{X}` in your template file.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

_TEMPLATES_DIR = Path(__file__).parent / "templates" / "reflect_response"


@lru_cache(maxsize=8)
def _load_template(name: str) -> str:
    """Load a reflect-response template by short name. Cached. Falls
    back to the plain default acknowledgment if the file is missing
    (preserves the tool's contract without crashing)."""
    path = _TEMPLATES_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError:
        return "Reflection noted."


def format_time_info(state: Any) -> str:
    """Absolute UTC timestamp + wall-clock elapsed since the agent
    started. Gives the agent a sense of time for deadline reasoning
    and for sizing `agent_await(timeout=…)` decisions."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    elapsed = int(time.time() - state.wall_start) if state.wall_start else 0
    if elapsed < 60:
        elapsed_str = f"{elapsed}s"
    elif elapsed < 3600:
        m, s = divmod(elapsed, 60)
        elapsed_str = f"{m}m{s}s" if s else f"{m}m"
    else:
        h, rem = divmod(elapsed, 3600)
        m, _ = divmod(rem, 60)
        elapsed_str = f"{h}h{m}m" if m else f"{h}h"
    return f"Now: {now_utc} · {elapsed_str} since agent start."


def render(template_name: str, *, state: Any, children: Any = None) -> str:
    """Render the reflect tool-result string for a given template.

    `state` is the agent's BudgetState; `children` is the snapshot
    list of subagent dicts (same shape Agent._meta_budget_stats
    builds — see budget_stats.format_budget_stats).

    For the `default` template, no placeholders are referenced and
    both kwargs are ignored — the template is just "Reflection
    noted." (current behavior). For richer templates the loader
    resolves each `{X}` placeholder by calling the matching
    internal-API function below."""
    template = _load_template(template_name)
    if "{" not in template:
        # Fast path — no placeholders. Default template hits this.
        return template
    from .budget_stats import format_budget_stats
    return template.format(
        time_info=format_time_info(state) if state is not None else "",
        budget_stats=(format_budget_stats(state, children=children)
                      if state is not None else ""),
    )
