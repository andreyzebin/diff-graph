"""`budget_stats` tool — formatter + builtin registration.

Phase 1 of agent-side budget planning (§12 design discussion). The
tool surfaces the agent's own session vs shared-with-children split
so the prompt can reason about spawn-vs-direct trade-offs.

This file pins three things:

- `format_budget_stats` produces a stable two-section text from a
  `BudgetState` — own-session line + shared-pool line + typical-
  spawn line. Format is what the agent reads, so changes here are
  agent-visible — guard it.
- Cold-start cases (no max_context configured, zero usage) come
  through without numeric errors or blanks.
- Builtin registration via `register_builtins` only happens when
  `"budget_stats"` is in the agent's tool list — opt-in per prompt,
  not on by default.
"""
from __future__ import annotations

import pytest

from orchestra.budget import BudgetState
from orchestra.budget_stats import format_budget_stats
from orchestra.tools.builtin import register_builtins
from orchestra.tools.registry import ToolRegistry


# ── format_budget_stats — pure formatter ────────────────────────────

class TestFormatter:

    def _state(self, **kw) -> BudgetState:
        defaults = dict(
            original_tokens=40_000,
            original_steps=40,
            original_max_context=128_000,
            tokens_in=0,
            cumulative_paid=0,
            steps_used=0,
            wall_start=0.0,
        )
        defaults.update(kw)
        return BudgetState(**defaults)

    def test_compact_table_lines_present(self):
        """Output is a 4-row compact table: own / shared / wall +
        blank + spawn-cost. Each axis line starts with a fixed
        prefix so prompts / scripts can grep for it. Wording drift
        on these prefixes breaks every prompt that opted into
        `with_state` reflects."""
        out = format_budget_stats(self._state())
        lines = out.splitlines()
        # 3 axis lines, blank separator, 1 spawn line = 5 lines.
        assert len(lines) == 5, f"expected 5 lines, got {len(lines)}: {lines}"
        assert lines[0].startswith("own ctx     ")
        assert lines[1].startswith("shared pool ")
        assert lines[2].startswith("wall clock  ")
        assert lines[3] == ""
        assert lines[4].startswith("spawn:")

    def test_own_line_shows_ratio_and_bar(self):
        """own line carries `tokens_in / max_context  <bar> <pct>%`.
        Bar must render at least one ▰ at non-zero usage so the
        signal is visible at a glance."""
        out = format_budget_stats(self._state(
            tokens_in=64_000, original_max_context=128_000,
        ))
        line = out.splitlines()[0]
        assert "64K" in line
        assert "128K" in line
        assert "50%" in line
        # Bar has filled cells at 50%
        assert "▰" in line
        assert "▱" in line

    def test_none_max_context_falls_back_to_default(self):
        """Defensive: if a state is constructed with
        `original_max_context=None` (no production path does this —
        BudgetConfig defaults to 128_000 — but tests / direct
        construction can), the formatter falls back to 128K rather
        than crashing on the divide-by-None."""
        out = format_budget_stats(self._state(
            tokens_in=12_000, original_max_context=None,
        ))
        line = out.splitlines()[0]
        assert "12K" in line
        assert "128K" in line
        # 12000 / 128000 = ~9%
        assert "9%" in line

    def test_shared_line_shows_tokens_and_steps(self):
        out = format_budget_stats(self._state(
            cumulative_paid=15_000, original_tokens=40_000,
            steps_used=8, original_steps=40,
        ))
        line = out.splitlines()[1]
        assert "15K" in line
        assert "40K" in line
        # Steps live in a parenthetical at the end of the shared line.
        assert "(steps 8/40)" in line

    def test_wall_line_without_max_wall_time(self):
        """No `max_wall_time` configured — wall line still renders
        elapsed, max column shows `—`, bar empty, pct=0."""
        import time as _t
        out = format_budget_stats(self._state(
            wall_start=_t.time() - 75,
            original_wall_time=None,
        ))
        line = out.splitlines()[2]
        assert line.startswith("wall clock  ")
        # Elapsed is rendered as a human time string (75s → "1m15s").
        assert "1m15s" in line
        # No-cap sentinel.
        assert "—" in line
        assert "0%" in line

    def test_wall_line_with_max_wall_time(self):
        """`max_wall_time` configured — wall line shows ratio."""
        import time as _t
        out = format_budget_stats(self._state(
            wall_start=_t.time() - 180,    # 3 minutes elapsed
            original_wall_time=600,        # 10 minute cap
        ))
        line = out.splitlines()[2]
        assert "3m" in line       # elapsed
        assert "10m" in line      # cap
        assert "30%" in line      # 3m/10m = 30%
        assert "▰" in line        # bar shows fill

    def test_spawn_line_carries_compact_estimates(self):
        """Spawn-cost row is one line: `spawn: ~<carved> tokens +
        ~<carved_steps> steps → ~<return> back`. Bare numeric
        ranges only — no disclaimer prose ('rough estimate;
        calibrated…') because the prompt would see it on every
        reflect."""
        out = format_budget_stats(self._state())
        line = out.splitlines()[4]
        assert line.startswith("spawn:")
        # Default messages.yaml values land verbatim.
        assert "20-30K" in line
        assert "10-20" in line
        assert "3-5K" in line
        # Arrow → in the "X back" half.
        assert "→" in line
        # No tutorial prose on every reflect.
        assert "rough estimate" not in line.lower()

    def test_zero_usage_renders_cleanly(self):
        """Fresh start (step 0) — all counters at 0. No division-by-zero,
        no blanks. Bars are fully empty."""
        out = format_budget_stats(self._state())
        # Both shared-line steps 0/40 and pct 0% visible.
        assert "(steps 0/40)" in out
        assert "0%" in out

    def test_number_formatting_compact(self):
        """K-suffix on >=1000, plain int otherwise."""
        out = format_budget_stats(self._state(
            tokens_in=2_500, original_max_context=128_000,
            cumulative_paid=750, original_tokens=40_000,
        ))
        # 2500 → "2.5K", 750 → "750" (plain), 128000 → "128K"
        assert "2.5K" in out
        assert "750" in out

    def test_no_children_subagents_block_absent(self):
        """`children=None` (the default) and `children=[]` both render
        WITHOUT the Subagents header — agents that haven't spawned
        anything shouldn't see noise about it."""
        out_none = format_budget_stats(self._state())
        out_empty = format_budget_stats(self._state(), children=[])
        for out in (out_none, out_empty):
            assert "Subagents" not in out
            # 5 lines (own / shared / wall / blank / spawn) — four
            # newlines, no trailing blank from the `{subagents}`
            # placeholder when it's empty.
            assert out.count("\n") == 4

    def test_children_block_lists_each_subagent(self):
        """Per-child line carries name, status, steps, context,
        paid, and (optionally) focus. Multi-child runs show one
        line per spawn."""
        out = format_budget_stats(self._state(), children=[
            {
                "name": "investigator",
                "focus": "check tax recompute",
                "status": "completed",
                "steps_used": 8,
                "tokens_in": 4_500,
                "cumulative_paid": 6_200,
            },
            {
                "name": "investigator",
                "focus": "verify ownership flow",
                "status": "running",
                "steps_used": 3,
                "tokens_in": 1_800,
                "cumulative_paid": 2_100,
            },
        ])
        # Header with the count.
        assert "Subagents (2 spawned):" in out
        # Each child's line carries the load-bearing facts. Per-line
        # field order is fixed (name [status] · steps · context · paid
        # · focus) so prompts can rely on consistent shape.
        assert "investigator [completed]" in out
        assert "investigator [running]" in out
        assert "8 steps" in out
        assert "3 steps" in out
        assert "~4.5K context" in out
        assert "~6.2K" in out      # paid for child 1
        assert 'focus="check tax recompute"' in out
        assert 'focus="verify ownership flow"' in out

    def test_long_focus_truncated_with_ellipsis(self):
        """Focus strings can be paragraph-long — truncate to keep
        the block scannable. The full focus is preserved in spawn
        events; this is just the rendered summary."""
        long_focus = "a" * 200
        out = format_budget_stats(self._state(), children=[{
            "name": "investigator",
            "focus": long_focus,
            "status": "completed",
            "steps_used": 1, "tokens_in": 100, "cumulative_paid": 200,
        }])
        # Truncated at 80 chars with an ellipsis (79 chars + …).
        assert "…" in out
        # The 200-char raw focus is NOT all in the output.
        assert "a" * 200 not in out

    def test_child_with_empty_focus_omits_focus_clause(self):
        """A focus= clause only appears when focus is non-empty —
        agents that spawn without a focus shouldn't get a noisy
        empty quote in the rendering."""
        out = format_budget_stats(self._state(), children=[{
            "name": "investigator",
            "focus": "",
            "status": "completed",
            "steps_used": 5, "tokens_in": 1_000, "cumulative_paid": 2_000,
        }])
        assert "investigator [completed]" in out
        assert "focus=" not in out


# ── Builtin registration is opt-in ──────────────────────────────────

class TestBuiltinRegistration:
    """`budget_stats` ships as a builtin but is only registered when
    the agent's tools list includes it — same opt-in pattern as
    agent_spawn / agent_list. Production prompts that don't ask for
    it shouldn't see it in their tool surface."""

    def _agent_config(self, tools: list[str]):
        from orchestra.types import AgentConfig, AgentMode, BudgetConfig, LLMParamsConfig
        return AgentConfig(
            name="probe",
            system_prompt="probe",
            user_prompt="probe",
            mode=AgentMode.REACT,
            reflect={'interval': 3},
            tools=tools,
            budget=BudgetConfig(),
            llm_params=LLMParamsConfig(),
        )

    def test_not_registered_without_opt_in(self):
        reg = ToolRegistry()
        register_builtins(reg, self._agent_config(tools=["done"]), agent=None)
        assert not reg.has("budget_stats")

    def test_registered_when_in_tools_list(self):
        reg = ToolRegistry()
        register_builtins(
            reg, self._agent_config(tools=["budget_stats", "done"]),
            agent=None,
        )
        assert reg.has("budget_stats")
        td = reg.get("budget_stats")
        # No required params — agent can call with no args.
        assert td.parameters.get("required", []) == []

    def test_dispatch_through_agent_meta_method(self):
        """When the registry has the tool AND an agent is passed, the
        handler delegates to `agent._meta_budget_stats`. Verifies the
        full plumbing — stops a future refactor that drops the
        `_meta_` prefix from quietly breaking the tool."""
        class _StubAgent:
            def _meta_budget_stats(self, args):
                return "STUB_OK"
        reg = ToolRegistry()
        register_builtins(
            reg, self._agent_config(tools=["budget_stats"]),
            agent=_StubAgent(),
        )
        out = reg.dispatch("budget_stats", {})
        assert out == "STUB_OK"

    def test_placeholder_handler_without_agent(self):
        """Without an agent, the registration still happens (so the
        schema appears) but the handler returns a placeholder rather
        than crashing. Matches agent_spawn / agent_list behaviour."""
        reg = ToolRegistry()
        register_builtins(
            reg, self._agent_config(tools=["budget_stats"]),
            agent=None,
        )
        out = reg.dispatch("budget_stats", {})
        # Placeholder string — exact wording isn't load-bearing but it
        # must not crash and must not look like real stats.
        assert isinstance(out, str)
        assert "Your own session" not in out
