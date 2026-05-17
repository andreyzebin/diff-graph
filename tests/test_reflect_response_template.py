"""Reflect tool-result templating — internal-API composition.

The `reflect` builtin returns a string the agent sees as its
tool_result. Default template is the legacy `Reflection noted.`;
opt-in `with_state` renders a state snapshot from internal APIs
(format_budget_stats, format_time_info) via the Jinja template
engine.

Pinned here:
- Default template = current behavior (backward compat).
- with_state template renders all the snapshot blocks (time +
  budget + subagents).
- Missing template file falls back gracefully (no crash).
- AgentConfig.flags["with_state"] toggles the variant
  (see TODO §19 — generic flags map replaced the dedicated
  reflect_response_template field).
- format_time_info: UTC + elapsed time formatting.
"""
from __future__ import annotations

import time

import pytest

from orchestra.budget import BudgetState
from orchestra.reflect_response import (
    format_time_info,
    render,
)


def _state(**kw) -> BudgetState:
    defaults = dict(
        original_tokens=40_000,
        original_steps=40,
        original_max_context=16_000,
        tokens_in=5_000,
        cumulative_paid=1_000,
        steps_used=3,
        wall_start=time.time() - 30,    # 30s elapsed
    )
    defaults.update(kw)
    return BudgetState(**defaults)


# ── Default template — backward compat ─────────────────────────────


class TestDefaultTemplate:

    def test_default_is_unchanged_acknowledgment(self):
        """The `default` template is the original "Reflection noted."
        — agents that DON'T opt into a richer template must see the
        same string they always saw. This is the load-bearing
        backward-compat guarantee."""
        out = render("default", state=_state())
        assert out == "Reflection noted."

    def test_default_ignores_state_kwarg(self):
        """Default has no placeholders, so passing state/children
        changes nothing in the output."""
        out_a = render("default", state=_state())
        out_b = render("default", state=_state(tokens_in=12_000))
        out_c = render("default", state=None, children=None)
        assert out_a == out_b == out_c == "Reflection noted."

    def test_missing_template_falls_back(self):
        """A template name that doesn't have a file falls back to
        the plain `Reflection noted.` — preserves the tool's
        contract if someone deletes a template by accident."""
        out = render("does_not_exist_anywhere", state=_state())
        assert out == "Reflection noted."


# ── with_state template — composed snapshot ────────────────────────


class TestWithStateTemplate:

    def test_renders_time_info_block(self):
        """The `with_state` template includes a `{time_info}`
        placeholder backed by format_time_info — agents see UTC
        timestamp + elapsed since agent start every reflect."""
        out = render("with_state", state=_state())
        assert "Now:" in out
        assert "UTC" in out
        assert "since agent start" in out

    def test_renders_budget_stats_block(self):
        """The `with_state` template includes a `{budget_stats}`
        placeholder backed by format_budget_stats — agents see own
        context, shared pool, wall-clock, spawn cost every reflect
        (and a subagents block if any children). Layout is the
        compact 3-axis table with bars (see test_budget_stats_tool
        for the row-by-row contract)."""
        out = render("with_state", state=_state())
        assert "own ctx     " in out
        assert "shared pool " in out
        assert "wall clock  " in out
        assert "spawn:" in out
        # Bars carry the at-a-glance signal.
        assert "▰" in out or "▱" in out

    def test_reflection_noted_preserved_at_top(self):
        """The leading "Reflection noted." line is preserved — the
        with_state variant ADDS state below it, doesn't replace it.
        Agents that grep tool_results for "Reflection noted" keep
        working."""
        out = render("with_state", state=_state())
        assert out.startswith("Reflection noted."), out[:40]

    def test_renders_subagents_when_present(self):
        """`children=[...]` flows into format_budget_stats and the
        subagents block appears in the with_state output."""
        out = render("with_state", state=_state(), children=[{
            "name": "investigator",
            "focus": "check tax",
            "status": "completed",
            "steps_used": 5,
            "tokens_in": 2_000,
            "cumulative_paid": 3_000,
        }])
        assert "Subagents (1 spawned)" in out
        assert "investigator [completed]" in out
        assert 'focus="check tax"' in out

    def test_no_children_skips_subagents_block(self):
        """`children=None` (or empty) means no Subagents header —
        avoids noise on runs that haven't spawned anything."""
        out = render("with_state", state=_state(), children=None)
        assert "Subagents" not in out


# ── format_time_info — UTC + elapsed ───────────────────────────────


class TestFormatTimeInfo:

    def test_includes_utc_timestamp(self):
        out = format_time_info(_state())
        assert "UTC" in out
        # Date in YYYY-MM-DD form
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}", out)

    def test_elapsed_under_a_minute(self):
        out = format_time_info(_state(wall_start=time.time() - 5))
        assert "5s since agent start" in out

    def test_elapsed_minutes(self):
        out = format_time_info(_state(wall_start=time.time() - 125))
        # 2m5s
        assert "2m5s since agent start" in out

    def test_elapsed_hours(self):
        out = format_time_info(_state(wall_start=time.time() - 3700))
        # 1h1m
        assert "1h1m since agent start" in out

    def test_no_wall_start_treats_as_zero(self):
        """Defensive: state with wall_start=0 renders as 0s
        elapsed, not crashes or shows wall-clock since epoch."""
        out = format_time_info(_state(wall_start=0))
        assert "0s since agent start" in out


# ── AgentConfig.reflect toggle ─────────────────────────────────────


class TestAgentConfigToggle:
    """Reflect template + cadence both live under the generic
    `reflect:` namespace on AgentConfig (mirrors `budget:` /
    `llm:` shape). Reflect handler reads
    `config.reflect.get("with_state")`."""

    def test_default_reflect_empty(self):
        from orchestra.types import AgentConfig
        cfg = AgentConfig(
            name="probe", system_prompt="x", user_prompt="y",
        )
        # No key set → reflect handler falls through to the
        # "Reflection noted." default template.
        assert cfg.reflect == {}

    def test_reflect_set_via_constructor(self):
        from orchestra.types import AgentConfig
        cfg = AgentConfig(
            name="probe", system_prompt="x", user_prompt="y",
            reflect={"with_state": True},
        )
        assert cfg.reflect["with_state"] is True


# ── user-message override frontmatter wins ─────────────────────────


class TestUserMessageOverridePlumbing:
    """A per-run `user_message_from` override carries its own
    frontmatter. When it sets `reflect:`, those keys win per-key
    over base config — otherwise bench/test prompts that opt
    into a feature would be silently ignored."""

    def test_override_reflect_replaces_config_default(self, tmp_path):
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
        )
        override = (
            "---\n"
            "reflect:\n"
            "  with_state: true\n"
            "---\n"
            "user body here\n"
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        assert a.config.reflect.get("with_state") is True

    def test_override_without_reflect_keeps_base(self, tmp_path):
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
            reflect={"with_state": True},
        )
        override = "---\ntools: [done]\n---\nbody\n"
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        # Override didn't mention reflect — base config value
        # preserved (no spurious reset).
        assert a.config.reflect.get("with_state") is True

    def test_override_reflect_merges_per_key(self):
        """Per-key merge: override's reflect keys update base's,
        leaving unmentioned keys alone. Mirrors how dict.update()
        works — the simplest semantics for "the override wins"."""
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
            reflect={"with_state": True, "other": "kept"},
        )
        override = (
            "---\n"
            "reflect:\n"
            "  with_state: false\n"
            "---\n"
            "body\n"
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        assert a.config.reflect["with_state"] is False
        assert a.config.reflect["other"] == "kept"


# ── budget override via user-message frontmatter ───────────────────


class TestBudgetOverridePlumbing:
    """`budget:` block in per-run user-message frontmatter overrides
    fields on AgentConfig.budget. Same channel as
    reflect_response_template — bench/test prompts shape the
    agent for THIS scenario without env vars or fixture yaml
    fields. Unspecified axes inherit base values."""

    def test_max_context_override_applies(self):
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig, BudgetConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
            budget=BudgetConfig(max_context=128_000, max_tokens=40_000),
        )
        override = "---\nbudget:\n  max_context: 16000\n---\nbody\n"
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        assert a.config.budget.max_context == 16_000
        # Other axes untouched.
        assert a.config.budget.max_tokens == 40_000

    def test_override_supports_all_known_axes(self):
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig, BudgetConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
            budget=BudgetConfig(),
        )
        override = (
            "---\n"
            "budget:\n"
            "  max_tokens: 12345\n"
            "  max_steps: 7\n"
            "  max_context: 9000\n"
            "  max_wall_time: 30.5\n"
            "---\nbody\n"
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        assert a.config.budget.max_tokens == 12345
        assert a.config.budget.max_steps == 7
        assert a.config.budget.max_context == 9_000
        assert a.config.budget.max_wall_time == 30.5

    def test_no_budget_block_keeps_base(self):
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig, BudgetConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
            budget=BudgetConfig(max_context=42_000),
        )
        override = "---\ntools: [done]\n---\nbody\n"
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        assert a.config.budget.max_context == 42_000


# ── {budget_stats_legend} placeholder ──────────────────────────────


class TestBudgetStatsLegendPlaceholder:
    """`{budget_stats_legend}` is a framework-provided placeholder
    that prompts opting into `with_state` reflects can embed to
    teach the agent how to read the compact snapshot layout once
    (in its first user message) instead of repeating the legend in
    every reflect tool-result."""

    def test_load_legend_returns_nonempty(self):
        from orchestra.budget_stats import load_legend
        out = load_legend()
        # Legend names every row label the snapshot uses — drift
        # here means the agent's first user message stops matching
        # the table it'll see in later reflects.
        assert "own ctx" in out
        assert "shared pool" in out
        assert "wall clock" in out
        assert "spawn:" in out

    def test_placeholder_resolves_in_user_message(self):
        """When a user-message body references `{budget_stats_legend}`,
        Agent._build_messages substitutes the loaded legend in place
        of the literal placeholder before the LLM sees it."""
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys",
            user_prompt=(
                "Task body.\n\n{{ budget_stats_legend }}\n\nThen do X."
            ),
            tools=["reflect", "done"],
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
        )
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        # Placeholder literal must NOT survive render.
        assert "{{ budget_stats_legend }}" not in user["content"]
        # And the actual legend content must be inlined.
        assert "own ctx" in user["content"]

    def test_prompts_without_placeholder_unchanged(self):
        """The legend is always loaded into the interpolation scope
        (cheap, cached), but prompts that don't reference the
        placeholder are unaffected — interpolation only substitutes
        when `{budget_stats_legend}` literally appears."""
        from orchestra.agent import Agent
        from orchestra.types import AgentConfig
        from orchestra.tools.registry import ToolRegistry

        cfg = AgentConfig(
            name="probe", system_prompt="sys",
            user_prompt="Plain body with no placeholder.",
            tools=["reflect", "done"],
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
        )
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        assert user["content"].strip() == "Plain body with no placeholder."
