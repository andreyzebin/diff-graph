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

from functools import lru_cache
from pathlib import Path

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
            "Typical spawn: returns {returned_to_you}; carves "
            "{carved_from_shared} ({based_on})."
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


def format_budget_stats(state: BudgetState) -> str:
    """Render the agent-facing budget summary string. Pure state — no
    instructions, no prescriptions. The agent's prompt is what tells
    the model how to react to the numbers.

    `max_context` always renders against a concrete value — production
    BudgetConfig defaults to 128_000 (see `types.py`). Defensive
    fallback to the same number here for the edge case where a
    state was constructed with `original_max_context=None`."""
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
        # Typical-spawn vars (from messages.yaml)
        returned_to_you=_TYPICAL_SPAWN_RETURN,
        carved_from_shared=_TYPICAL_SPAWN_CARVED,
        based_on=_TYPICAL_SPAWN_SOURCE,
    )
