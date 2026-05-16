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

    def test_four_sections_present(self):
        """The summary has exactly four lines (in order): own session,
        shared pool, wall-clock, typical spawn. The prompt depends on
        this shape when it weaves the output into its planning
        paragraph; wall-clock sits between the consumption summary
        and the spawn-cost reference because it ticks INDEPENDENTLY
        of work — agents reasoning about agent_await need it visible."""
        out = format_budget_stats(self._state())
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 4, f"expected 4 sections, got {len(lines)}"
        assert lines[0].startswith("Your own session:")
        assert lines[1].startswith("Shared with children:")
        assert lines[2].startswith("Wall-clock")
        assert lines[3].startswith("Typical investigator spawn:")

    def test_own_session_with_max_context(self):
        """When max_context is configured the line shows ratio + a
        plain-English note that spawning offloads to a child window."""
        out = format_budget_stats(self._state(
            tokens_in=64_000, original_max_context=128_000,
        ))
        line = out.splitlines()[0]
        assert "64K" in line
        assert "128K" in line
        assert "50%" in line
        # Spawn semantic — children spawn into fresh windows — is a
        # load-bearing piece the prompt relies on when explaining
        # the trade-off. If wording drifts, prompt-side hints break.
        assert "fresh window" in line.lower() or "fresh windows" in line.lower()

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
        # Spawn-semantic note still appears (single template).
        assert "fresh window" in line.lower()

    def test_shared_pool_line_shows_tokens_and_steps(self):
        out = format_budget_stats(self._state(
            cumulative_paid=15_000, original_tokens=40_000,
            steps_used=8, original_steps=40,
        ))
        line = out.splitlines()[1]
        assert "15K" in line
        assert "40K" in line
        assert "8 of 40 steps" in line
        # The "carves a slice" framing is what tells the model
        # spawning costs from THIS pool — pin it.
        assert "carve" in line.lower()

    def test_wall_clock_line_without_max_wall_time(self):
        """No `max_wall_time` configured (the default) — wall line
        still renders with elapsed seconds + an explicit "no cap"
        note. Agents need elapsed time visible for agent_await
        decisions even when there's no hard wall budget."""
        import time as _t
        # Backdate wall_start by 75 seconds so elapsed renders as
        # something human-readable.
        out = format_budget_stats(self._state(
            wall_start=_t.time() - 75,
            original_wall_time=None,
        ))
        line = next(ln for ln in out.splitlines() if ln.startswith("Wall-clock"))
        # Phrasing names the load-bearing fact for await reasoning.
        assert "ticks even during await" in line
        # Elapsed is rendered as a human time string (75s → "1m15s").
        assert "1m15s" in line
        assert "no max_wall_time cap configured" in line

    def test_wall_clock_line_with_max_wall_time(self):
        """`max_wall_time` configured — wall line shows ratio."""
        import time as _t
        out = format_budget_stats(self._state(
            wall_start=_t.time() - 180,    # 3 minutes elapsed
            original_wall_time=600,        # 10 minute cap
        ))
        line = next(ln for ln in out.splitlines() if ln.startswith("Wall-clock"))
        assert "ticks even during await" in line
        assert "3m" in line       # elapsed
        assert "10m" in line      # cap
        assert "30%" in line      # 3m/10m = 30%

    def test_typical_spawn_line_carries_estimate_disclaimer(self):
        """Phase 1 hardcoded values explicitly tagged as rough. The
        disclaimer is part of the contract — the model treats the
        numbers as heuristics, not precise budgets."""
        out = format_budget_stats(self._state())
        # Typical-spawn is the 4th line (after own / shared / wall).
        line = out.splitlines()[3]
        assert "rough estimate" in line.lower()

    def test_zero_usage_renders_cleanly(self):
        """Fresh start (step 0) — all counters at 0. No division-by-zero,
        no blanks."""
        out = format_budget_stats(self._state())
        assert "0 of 40 steps" in out
        assert "0%" in out

    def test_number_formatting_compact(self):
        """K-suffix on >=1000, plain int otherwise. Keeps the line
        readable at a glance."""
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
            # Four sections (own / shared / wall / typical) — three
            # newlines, no trailing blank from the `{subagents}`
            # placeholder when it's empty.
            assert out.count("\n") == 3

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
            reflect_interval=3,
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
