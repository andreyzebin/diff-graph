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

Typical-spawn values are HARDCODED rough estimates for Phase 1 —
calibrated once §11 (repo memory) or §12 (per-bucket aggregation)
land and we have real measured medians. The output explicitly tags
this as a rough estimate so the model treats it as a heuristic, not
a precise budget.
"""
from __future__ import annotations

from .budget import BudgetState
from .messages import msg


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


def format_budget_stats(state: BudgetState) -> str:
    """Render the agent-facing budget summary string. Pure state — no
    instructions, no prescriptions. The agent's prompt is what tells
    the model how to react to the numbers."""

    # ── Your own session ─────────────────────────────────────────
    # Context = the agent's own LLM input window. tokens_in is the
    # latest prompt_tokens reading (= current conversation size).
    if state.original_max_context and state.original_max_context > 0:
        own_session = (
            f"Your own session: {_fmt_k(state.tokens_in)} of "
            f"{_fmt_k(state.original_max_context)} LLM context window used "
            f"({_pct(state.tokens_in, state.original_max_context)}). "
            f"Children spawn into fresh windows; only their done() summary "
            f"returns to your session."
        )
    else:
        own_session = (
            f"Your own session: {_fmt_k(state.tokens_in)} LLM context tokens "
            f"used (no max_context configured). Children spawn into fresh "
            f"windows; only their done() summary returns here."
        )

    # ── Shared with children ─────────────────────────────────────
    # cumulative_paid currently tracks per-step paid (see budget.py
    # update_tokens) — but for the agent the meaningful framing is
    # "this is the pool you share with your spawns". Each spawn
    # carves a slice via allocate_child.
    shared = (
        f"Shared with children: {_fmt_k(state.cumulative_paid)} of "
        f"{_fmt_k(state.original_tokens)} tokens, "
        f"{state.steps_used} of {state.original_steps} steps used. "
        f"Each agent_spawn carves a slice from your remaining budget."
    )

    # ── Typical spawn cost (Phase 1: hardcoded estimates) ───────
    typical = (
        f"Typical investigator spawn: returns "
        f"{_TYPICAL_SPAWN_RETURN} to your own session; carves "
        f"{_TYPICAL_SPAWN_CARVED} from the shared pool "
        f"({_TYPICAL_SPAWN_SOURCE})."
    )

    return "\n".join([own_session, shared, typical])
