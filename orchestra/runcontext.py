"""RunContext — single source of truth for variables exposed to
prompt templates.

Replaces the ad-hoc `framework_vars` dict that
`agent.py::_build_messages` used to assemble in line. Owned by
the Agent for the duration of one run; lazy properties compute
on demand so prompts that don't reference (say)
`{{ budget_stats }}` don't pay for rendering it.

Consumed by `orchestra.template_engine.render(text, ctx)` via
`ctx.to_kwargs()` — flat dict of {var_name: value} the Jinja
Environment binds at render time.

Adding a new framework variable:
1. New `@property` here (or new field if it's plain data).
2. Reference it in `to_kwargs()`.
3. Templates use `{{ name }}`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional


if TYPE_CHECKING:
    from .budget import BudgetState


@dataclass
class RunContext:
    """One per Agent run. Constructed in Agent._build_messages
    (or earlier and stashed) from already-resolved state."""

    # ── Data scope (resolved @data fields + caller's prompt_vars) ─
    # Most common keys: pr_title, pr_description, commits, focus,
    # message, comment_thread, jira_tickets, … — whatever the
    # prompt's `data:` block declared, plus any extras the caller
    # injected via prompt_vars.
    data: dict[str, Any] = field(default_factory=dict)

    # ── Agent identity ─────────────────────────────────────────
    agent_name: str = ""
    agent_id: str = ""
    depth: int = 0

    # ── Live state (set by the Agent before render) ───────────
    budget_state: Optional["BudgetState"] = None
    # Snapshot of subagent dicts (name / focus / status / steps /
    # tokens_in / cumulative_paid). Reflect handler builds this
    # under _children_lock before rendering. None when the agent
    # has no children — the budget_stats template's
    # `{% if children %}` collapses the subagents block.
    children_snapshot: Optional[list[dict]] = None
    # Aggregated skill rationale bodies — set by Agent.__init__
    # after mount_skills().
    skills_body: str = ""

    # ── Lazy framework helpers ─────────────────────────────────

    @property
    def budget_stats_legend(self) -> str:
        from .budget_stats import load_legend
        return load_legend()

    @property
    def budget_stats(self) -> str:
        """Compact-table snapshot. None-safe if budget_state
        wasn't supplied — returns empty string."""
        if self.budget_state is None:
            return ""
        from .budget_stats import format_budget_stats
        return format_budget_stats(
            self.budget_state, children=self.children_snapshot,
        )

    @property
    def time_info(self) -> str:
        if self.budget_state is None:
            return ""
        from .reflect_response import format_time_info
        return format_time_info(self.budget_state)

    @property
    def skills(self) -> str:
        return self.skills_body

    # ── Per-area config blocks ────────────────────────────────
    # The agent's effective AgentConfig.reflect map (and future
    # per-area blocks) — merged from base prompt + skills +
    # per-run override at Agent.__init__. Templates can read
    # individual keys with `{{ reflect.with_state }}` or branch
    # with `{% if reflect.with_state %}`.
    reflect: dict = field(default_factory=dict)

    @property
    def context_pct(self) -> int:
        """0-100. Useful for `{% if context_pct > 75 %}` in
        Jinja-aware prompts. 0 when budget_state absent."""
        if self.budget_state is None:
            return 0
        bs = self.budget_state
        cap = bs.original_max_context or 0
        return int(round(100 * bs.tokens_in / cap)) if cap else 0

    @property
    def steps_used(self) -> int:
        return self.budget_state.steps_used if self.budget_state else 0

    # ── Compatibility shims ────────────────────────────────────

    def __getitem__(self, key: str) -> Any:
        """Mapping-style access for any legacy code that wants
        ctx[key]. Data scope wins over framework helpers (so a
        prompt's own `{x}` always means its own `x`)."""
        if key in self.data:
            return self.data[key]
        # Property/attribute fallback.
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return key in self.data or hasattr(self, key)

    def to_kwargs(self) -> dict[str, Any]:
        """Flat dict for `**kwargs` rendering. Data scope spread
        first, then framework names so collisions favour
        framework values (a `{{ skills }}` placeholder must
        always resolve to the mounted skill bodies, not to a
        stray data field someone named `skills`)."""
        return {
            **self.data,
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "depth": self.depth,
            "budget_stats": self.budget_stats,
            "budget_stats_legend": self.budget_stats_legend,
            "time_info": self.time_info,
            "skills": self.skills,
            "context_pct": self.context_pct,
            "steps_used": self.steps_used,
            "reflect": dict(self.reflect),
        }
