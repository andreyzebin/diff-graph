"""Format a budget summary for the `budget_stats` tool.

The summary is what the model sees when it calls `budget_stats()`. It
splits the agent's consumption into two conceptually distinct
groups so the prompt can reason about spawn-vs-direct trade-offs:

  - **Your own session**: the agent's LLM context window. Each agent
    has its own — children spawn into fresh windows, so the
    `done()` summary that returns to the parent is the only thing
    that grows the parent's session.

  - **Shared with children**: tokens and steps. Each spawn carves a
    slice from the parent's remaining budget (see
    `BudgetTracker.allocate_child`). Spawning costs from this pool;
    the parent's own session doesn't see those tokens directly.

The elegant invariant this framing exposes: **a spawn trades
shared-pool budget for own-context budget.** If context is filling,
spawn (child works in a fresh window, you get back a small summary).
If the shared pool is tight, spawning costs more than direct work.
If both are tight, consolidate. The prompt picks the response; this
function just surfaces the numbers.

Wording lives in `orchestra/templates/budget_stats/budget_stats.md`
(Jinja). The subagents block is an inline `{% for %}` loop in the
same template — no separate fragment files. `_bar()` is exposed as
a Jinja global so the template can call `{{ bar(part, whole) }}`
directly if it wants ad-hoc bars beyond the precomputed ones.

Typical-spawn values are HARDCODED rough estimates for Phase 1 —
calibrated once §11 (repo memory) or §12 (per-bucket aggregation)
land and we have real measured medians.
"""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .budget import BudgetState
from .messages import msg


_TEMPLATES_DIR = Path(__file__).parent / "templates" / "budget_stats"


# Hardcoded rough estimates for Phase 1. Replace with measured medians
# from traces.db once the aggregation layer (§12) lands. Bare numeric
# ranges — wording lives in the template.
_TYPICAL_SPAWN_CARVED = msg("budget_stats.typical_spawn.spawn_carved", "20-30K")
_TYPICAL_SPAWN_CARVED_STEPS = msg(
    "budget_stats.typical_spawn.spawn_carved_steps", "10-20",
)
_TYPICAL_SPAWN_RETURN = msg("budget_stats.typical_spawn.spawn_return", "3-5K")


# ── small helpers (some exposed to Jinja as globals; see template_engine) ─


def _fmt_k(n: int) -> str:
    """3500 → '3.5K'; 12000 → '12K'; 250 → '250'."""
    if n is None:
        return "—"
    if n >= 1_000:
        return f"{n/1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


_BAR_WIDTH = 10
_BAR_FULL = "▰"
_BAR_EMPTY = "▱"


def _bar(part: float, whole: float, width: int = _BAR_WIDTH) -> str:
    """Render a `width`-cell monospace progress bar. Clamps 0-100%;
    a fully-empty cap (`whole=0` / None) renders as an empty bar
    rather than crashing. Exposed as a Jinja global by
    `orchestra.template_engine`."""
    if not whole or whole <= 0:
        return _BAR_EMPTY * width
    filled = max(0, min(width, int(round(width * part / whole))))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


_DEFAULT_MAX_CONTEXT = 128_000
_FOCUS_TRUNCATE_AT = 80


def _fmt_seconds(s: int) -> str:
    """5 → '5s'; 90 → '1m30s'; 3700 → '1h1m'."""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec}s" if sec else f"{m}m"
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m}m" if m else f"{h}h"


def _wall_parts(state: BudgetState) -> tuple[str, str, str, int]:
    """Return `(elapsed_str, wall_max_str, wall_bar, wall_pct)`."""
    elapsed = int(time.time() - state.wall_start) if state.wall_start else 0
    elapsed_str = _fmt_seconds(elapsed)
    if state.original_wall_time and state.original_wall_time > 0:
        max_s = int(state.original_wall_time)
        pct = min(100, int(round(100 * elapsed / max_s))) if max_s > 0 else 0
        return elapsed_str, _fmt_seconds(max_s), _bar(elapsed, max_s), pct
    return elapsed_str, "—", _bar(0, 1), 0


def _prep_children(children: Optional[list[dict]]) -> list[dict]:
    """Pre-shape children dicts for the template:
    {name, status, steps_used, ctx_in, paid, focus}. Focus is
    truncated with ellipsis to keep the rendered block scannable.
    Empty / None → []."""
    if not children:
        return []
    out: list[dict] = []
    for c in children:
        focus = (c.get("focus") or "").strip()
        if len(focus) > _FOCUS_TRUNCATE_AT:
            focus = focus[: _FOCUS_TRUNCATE_AT - 1] + "…"
        out.append({
            "name": c.get("name", "?"),
            "status": c.get("status", "?"),
            "steps_used": c.get("steps_used", 0),
            "ctx_in": _fmt_k(c.get("tokens_in", 0)),
            "paid": _fmt_k(c.get("cumulative_paid", 0)),
            "focus": focus,
        })
    return out


@lru_cache(maxsize=1)
def load_legend() -> str:
    """One-line legend that explains the snapshot's row labels.
    Wording in `orchestra/templates/budget_stats/legend.md`.
    Embed via `{budget_stats_legend}` (legacy) or
    `{{ budget_stats_legend }}` (Jinja) in user prompts."""
    path = _TEMPLATES_DIR / "legend.md"
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError:
        return ""


def format_budget_stats(
    state: BudgetState,
    *,
    children: Optional[list[dict]] = None,
) -> str:
    """Render the agent-facing budget summary string via Jinja.
    Pure state — no instructions, no prescriptions; prompts pick
    the response.

    Compact-table layout with progress bars. Subagents block is
    an inline `{% for %}` loop in the template — empty children
    collapses the block cleanly via `{% if children %}`.

    `max_context` defensive fallback to the framework default
    (128_000) for the edge case where `original_max_context=None`
    (production BudgetConfig always has a value)."""
    max_context = state.original_max_context or _DEFAULT_MAX_CONTEXT
    elapsed_str, wall_max_str, wall_bar, wall_pct = _wall_parts(state)

    # Narrow kwargs specifically for the budget_stats template —
    # uses render_named_kwargs so framework-level RunContext
    # helpers (skills / context_pct / etc.) don't pollute or
    # shadow names like `steps_used`.
    from .template_engine import render_named_kwargs
    return render_named_kwargs(
        "budget_stats",
        tokens_in=_fmt_k(state.tokens_in),
        max_context=_fmt_k(max_context),
        own_bar=_bar(state.tokens_in, max_context),
        pct=int(round(100 * state.tokens_in / max_context)),
        paid=_fmt_k(state.cumulative_paid),
        max_tokens=_fmt_k(state.original_tokens),
        shared_bar=_bar(state.cumulative_paid, state.original_tokens),
        shared_pct=int(round(100 * state.cumulative_paid /
                             max(1, state.original_tokens))),
        steps_used=state.steps_used,
        max_steps=state.original_steps,
        elapsed=elapsed_str,
        wall_max=wall_max_str,
        wall_bar=wall_bar,
        wall_pct=wall_pct,
        spawn_carved=_TYPICAL_SPAWN_CARVED,
        spawn_carved_steps=_TYPICAL_SPAWN_CARVED_STEPS,
        spawn_return=_TYPICAL_SPAWN_RETURN,
        children=_prep_children(children),
    ).rstrip()
