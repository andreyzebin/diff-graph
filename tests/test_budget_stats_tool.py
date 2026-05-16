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

    def test_three_sections_present(self):
        """The summary has exactly three lines: own session, shared
        pool, typical spawn. The prompt depends on this shape when it
        weaves the output into its planning paragraph."""
        out = format_budget_stats(self._state())
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 3, f"expected 3 sections, got {len(lines)}"
        assert lines[0].startswith("Your own session:")
        assert lines[1].startswith("Shared with children:")
        assert lines[2].startswith("Typical investigator spawn:")

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

    def test_typical_spawn_line_carries_estimate_disclaimer(self):
        """Phase 1 hardcoded values explicitly tagged as rough. The
        disclaimer is part of the contract — the model treats the
        numbers as heuristics, not precise budgets."""
        out = format_budget_stats(self._state())
        line = out.splitlines()[2]
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
