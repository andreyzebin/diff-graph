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

Wording lives in `orchestra/templates/budget_stats/budget_stats.md`.
Edit that file to tune what the agent reads; this module just resolves
placeholders. `max_context` always has a value (BudgetConfig defaults
to 128_000) — no "no_max_context" variant exists by design.

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


@lru_cache(maxsize=8)
def _load(name: str) -> str:
    """Load a budget_stats template by short name (no extension).
    Cached. Source-of-truth lives in
    `orchestra/templates/budget_stats/<name>.md`. The main
    `budget_stats` template additionally has a `.fallback.md`
    backstop so a missing primary doesn't lose the contract — for
    every other template a missing file yields `""` (the rendered
    section just collapses to nothing). No template wording is
    duplicated in-code."""
    primary = _TEMPLATES_DIR / f"{name}.md"
    try:
        return primary.read_text(encoding="utf-8").rstrip()
    except OSError:
        fallback = _TEMPLATES_DIR / f"{name}.fallback.md"
        try:
            return fallback.read_text(encoding="utf-8").rstrip()
        except OSError:
            return ""


def _load_template() -> str:
    """Back-compat name for the main budget_stats template loader."""
    return _load("budget_stats") or "(budget_stats template missing)"


def _fmt_k(n: int) -> str:
    """3500 → '3.5K'; 12000 → '12K'; 250 → '250'. Keeps numbers
    human-scannable. The rstrip dance trims `.0` for whole-thousand
    values BEFORE appending K — otherwise the trailing letter blocks
    the strip."""
    if n is None:
        return "—"
    if n >= 1_000:
        return f"{n/1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


def _pct(part: float, whole: float) -> str:
    if not whole or whole <= 0:
        return "—"
    return f"{int(round(100 * part / whole))}%"


_BAR_WIDTH = 10
_BAR_FULL = "▰"   # ▰
_BAR_EMPTY = "▱"  # ▱


def _bar(part: float, whole: float, width: int = _BAR_WIDTH) -> str:
    """Render a `width`-cell monospace progress bar. Clamps 0-100%;
    a fully-empty cap (`whole=0` / None) renders as an empty bar
    rather than crashing. Used by the with_state snapshot to give
    the model a glance-level signal alongside the percent number."""
    if not whole or whole <= 0:
        return _BAR_EMPTY * width
    filled = max(0, min(width, int(round(width * part / whole))))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


_DEFAULT_MAX_CONTEXT = 128_000

# Focus strings can be long — truncate with ellipsis to keep the
# subagents block scannable. The agent has access to the full focus
# via its own spawn history; this is just the rendered summary.
_FOCUS_TRUNCATE_AT = 80


def _fmt_seconds(s: int) -> str:
    """5 → '5s'; 90 → '1m30s'; 3700 → '1h1m'. Wall-clock is most
    actionable as a human time scale, not as seconds-or-K-of-them."""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec}s" if sec else f"{m}m"
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m}m" if m else f"{h}h"


def _wall_parts(state: BudgetState) -> tuple[str, str, str, int]:
    """Return `(elapsed_str, wall_max_str, wall_bar, wall_pct)` for
    the compact-table wall line. Without `max_wall_time`: max
    column renders `—`, bar renders empty, pct=0. With cap: shows
    elapsed/cap and a filled bar."""
    elapsed = int(time.time() - state.wall_start) if state.wall_start else 0
    elapsed_str = _fmt_seconds(elapsed)
    if state.original_wall_time and state.original_wall_time > 0:
        max_s = int(state.original_wall_time)
        pct = min(100, int(round(100 * elapsed / max_s))) if max_s > 0 else 0
        return elapsed_str, _fmt_seconds(max_s), _bar(elapsed, max_s), pct
    return elapsed_str, "—", _bar(0, 1), 0


def _format_subagents(children: list[dict]) -> str:
    """Build the subagents block appended to the main summary. Empty
    string when no children — the template's trailing placeholder
    collapses to nothing.

    Each child dict carries: name, focus, status, steps_used,
    tokens_in, cumulative_paid (snapshotted by Agent._meta_budget_stats
    under the children lock).

    Wording lives in three template files under
    `orchestra/templates/budget_stats/`:
      - `subagents_block.md` — outer wrapper, `{count}` + `{items}`.
      - `subagents_item.md`  — per-child line, `{focus_clause}` is
        appended only when the focus is non-empty.
      - `subagents_focus.md` — the ` · focus="..."` clause.
    """
    if not children:
        return ""
    item_tpl = _load("subagents_item")
    focus_tpl = _load("subagents_focus")
    items: list[str] = []
    for c in children:
        focus = (c.get("focus") or "").strip()
        if len(focus) > _FOCUS_TRUNCATE_AT:
            focus = focus[: _FOCUS_TRUNCATE_AT - 1] + "…"
        focus_clause = focus_tpl.format(focus=focus) if focus else ""
        items.append(item_tpl.format(
            name=c.get("name", "?"),
            status=c.get("status", "?"),
            steps=c.get("steps_used", 0),
            ctx_in=_fmt_k(c.get("tokens_in", 0)),
            paid=_fmt_k(c.get("cumulative_paid", 0)),
            focus_clause=focus_clause,
        ))
    return _load("subagents_block").format(
        count=len(children),
        items="\n".join(items),
    )


def format_budget_stats(
    state: BudgetState,
    *,
    children: Optional[list[dict]] = None,
) -> str:
    """Render the agent-facing budget summary string. Pure state — no
    instructions, no prescriptions. The agent's prompt is what tells
    the model how to react to the numbers.

    `max_context` always renders against a concrete value — production
    BudgetConfig defaults to 128_000 (see `types.py`). Defensive
    fallback to the same number here for the edge case where a
    state was constructed with `original_max_context=None`.

    `children` (optional) — when this agent has spawned subagents,
    Agent._meta_budget_stats passes a snapshot list so the rendered
    summary shows per-child name / focus / status / consumption.
    Empty / None → the subagents block is omitted entirely."""
    max_context = state.original_max_context or _DEFAULT_MAX_CONTEXT
    elapsed_str, wall_max_str, wall_bar, wall_pct = _wall_parts(state)
    return _load_template().format(
        # Own-session
        tokens_in=_fmt_k(state.tokens_in),
        max_context=_fmt_k(max_context),
        own_bar=_bar(state.tokens_in, max_context),
        pct=int(round(100 * state.tokens_in / max_context)),
        # Shared pool — tokens line with steps in parentheses
        paid=_fmt_k(state.cumulative_paid),
        max_tokens=_fmt_k(state.original_tokens),
        shared_bar=_bar(state.cumulative_paid, state.original_tokens),
        shared_pct=int(round(100 * state.cumulative_paid /
                             max(1, state.original_tokens))),
        steps_used=state.steps_used,
        max_steps=state.original_steps,
        # Wall-clock — critical for agent_await(timeout=…) decisions:
        # real-time keeps ticking while the agent blocks on a child.
        elapsed=elapsed_str,
        wall_max=wall_max_str,
        wall_bar=wall_bar,
        wall_pct=wall_pct,
        # Typical-spawn — bare numeric ranges; wording in the template.
        spawn_carved=_TYPICAL_SPAWN_CARVED,
        spawn_carved_steps=_TYPICAL_SPAWN_CARVED_STEPS,
        spawn_return=_TYPICAL_SPAWN_RETURN,
        # Subagents block — empty string when no children.
        subagents=_format_subagents(children or []),
    )
