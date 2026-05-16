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
# from traces.db once the aggregation layer (§12) lands.
_TYPICAL_SPAWN_RETURN = msg(
    "budget_stats.typical_spawn.returned_to_you",
    "~3-5K (the done() summary)",
)
_TYPICAL_SPAWN_CARVED = msg(
    "budget_stats.typical_spawn.carved_from_shared",
    "~20-30K tokens, ~10-20 steps",
)
_TYPICAL_SPAWN_SOURCE = msg(
    "budget_stats.typical_spawn.based_on",
    "rough estimate; calibrated once measured stats land",
)


@lru_cache(maxsize=1)
def _load_template() -> str:
    """Load the single budget_stats template. Cached so repeated
    tool calls don't re-read the file. Falls back to a minimal
    inline string if the template file is missing (preserves the
    tool's contract without crashing if someone deletes the file)."""
    path = _TEMPLATES_DIR / "budget_stats.md"
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError:
        return (
            "Your own session: {tokens_in} of {max_context} context "
            "tokens used ({pct}%).\n"
            "Shared with children: {paid} of {max_tokens} tokens, "
            "{steps_used} of {max_steps} steps used.\n"
            "{wall_clock}\n"
            "Typical spawn: returns {returned_to_you}; carves "
            "{carved_from_shared} ({based_on}).{subagents}"
        )


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


def _format_wall_clock(state: BudgetState) -> str:
    """Render the wall-clock line. Always present — agents reasoning
    about `agent_await(timeout=…)` need to know the clock keeps
    ticking even while they block on children. With `max_wall_time`
    configured: shows ratio. Without: just elapsed (no cap)."""
    elapsed = int(time.time() - state.wall_start) if state.wall_start else 0
    elapsed_str = _fmt_seconds(elapsed)
    if state.original_wall_time and state.original_wall_time > 0:
        max_s = int(state.original_wall_time)
        pct = min(100, int(round(100 * elapsed / max_s))) if max_s > 0 else 0
        return (
            f"Wall-clock (ticks even during await): {elapsed_str} of "
            f"{_fmt_seconds(max_s)} used ({pct}%)."
        )
    return (
        f"Wall-clock (ticks even during await): {elapsed_str} elapsed "
        f"(no max_wall_time cap configured)."
    )


def _format_subagents(children: list[dict]) -> str:
    """Build the subagents block appended to the main summary. Empty
    string when no children — the template's trailing placeholder
    collapses to nothing.

    Each child dict carries: name, focus, status, steps_used,
    tokens_in, cumulative_paid (snapshotted by Agent._meta_budget_stats
    under the children lock)."""
    if not children:
        return ""
    lines = ["", f"Subagents ({len(children)} spawned):"]
    for c in children:
        name = c.get("name", "?")
        status = c.get("status", "?")
        steps = c.get("steps_used", 0)
        paid = _fmt_k(c.get("cumulative_paid", 0))
        ctx_in = _fmt_k(c.get("tokens_in", 0))
        focus = (c.get("focus") or "").strip()
        if len(focus) > _FOCUS_TRUNCATE_AT:
            focus = focus[: _FOCUS_TRUNCATE_AT - 1] + "…"
        # Plain-state per-child line: name [status] — steps · context
        # · paid · focus.
        bits = [f"  - {name} [{status}]",
                f"{steps} steps",
                f"~{ctx_in} context",
                f"paid ~{paid}"]
        line = " · ".join(bits)
        if focus:
            line += f' · focus="{focus}"'
        lines.append(line)
    return "\n".join(lines)


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
    return _load_template().format(
        # Own-session vars
        tokens_in=_fmt_k(state.tokens_in),
        max_context=_fmt_k(max_context),
        pct=int(round(100 * state.tokens_in / max_context)),
        # Shared-pool vars
        paid=_fmt_k(state.cumulative_paid),
        max_tokens=_fmt_k(state.original_tokens),
        steps_used=state.steps_used,
        max_steps=state.original_steps,
        # Wall-clock — own line, always rendered. Critical for
        # agent_await(timeout=…) decisions: real-time keeps ticking
        # while the agent blocks on a child.
        wall_clock=_format_wall_clock(state),
        # Typical-spawn vars (from messages.yaml)
        returned_to_you=_TYPICAL_SPAWN_RETURN,
        carved_from_shared=_TYPICAL_SPAWN_CARVED,
        based_on=_TYPICAL_SPAWN_SOURCE,
        # Subagents block — empty string when no children.
        subagents=_format_subagents(children or []),
    )
