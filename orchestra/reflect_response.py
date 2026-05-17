"""Reflect tool's tool-result string — rendered via the unified
template engine.

Each reflect call returns a string the model sees as the tool
result. Layout lives in `orchestra/templates/reflect_response/
<name>.md` (Jinja). Agents opt in to a non-default template via
`reflect_response_template:` in user-message frontmatter.

Two templates ship today:
- `default.md` — just "Reflection noted." (the legacy
  acknowledgment, kept for backward compat).
- `with_state.md` — renders `{{ time_info }}` + `{{ budget_stats
  }}` so the agent re-grounds on every reflect.

Adding a new template:
  1. Create `orchestra/templates/reflect_response/<name>.md`.
  2. Reference whatever RunContext properties you need
     (`{{ budget_stats }}`, `{{ skills }}`, `{{ context_pct }}`,
     custom data fields, …) — all available via the same
     RunContext used for the agent's user prompt.
  3. Opt in per-prompt: `reflect_response_template: <name>`.

The default template path stays fast (no template file open,
no Jinja parse) so prompts that don't opt in pay nothing.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TEMPLATES_DIR = Path(__file__).parent / "templates" / "reflect_response"


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


# Default template is the literal "Reflection noted." — fast-path
# so prompts that don't opt in pay nothing (no template open, no
# Jinja parse).
_DEFAULT_BODY = "Reflection noted."


def render(template_name: str, *, state: Any, children: Any = None) -> str:
    """Render the reflect tool-result string.

    `state` is the agent's BudgetState; `children` is the
    snapshot list of subagent dicts (same shape
    `format_budget_stats` consumes). Passed onto the
    RunContext so the template can reference `{{ time_info }}`,
    `{{ budget_stats }}`, etc."""
    if template_name == "default":
        return _DEFAULT_BODY
    # Defensive: missing file → fall back to default acknowledgment
    # so a typo in `reflect_response_template:` doesn't crash the
    # reflect contract.
    path = _TEMPLATES_DIR / f"{template_name}.md"
    if not path.exists():
        return _DEFAULT_BODY
    from .runcontext import RunContext
    from .template_engine import render_named
    ctx = RunContext(
        budget_state=state,
        children_snapshot=children,
    )
    return render_named(template_name, ctx).rstrip()
