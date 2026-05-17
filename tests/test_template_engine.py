"""Template engine — auto-detect between legacy `{x}` and Jinja
`{{ x }}`, plus the RunContext that both engines consume.

Pinned here:
- `render(text, ctx)` auto-detects: text with `{{` or `{%`
  goes through Jinja; everything else routes to the legacy
  `interpolate()` regex substitution.
- Both paths see the same flat variable namespace via
  `ctx.to_kwargs()` — no duplicate definitions.
- `render_named` loads a framework template (Jinja-only) against
  a RunContext.
- `render_named_kwargs` lets internal callers pass a plain
  kwargs dict — used by `format_budget_stats` so its narrow
  variable names (like `steps_used`) don't get shadowed by
  RunContext's framework-helper properties.
- RunContext properties (`skills`, `budget_stats_legend`,
  `context_pct`, …) are lazy and None-safe.

Backward-compat boundary: any prompt that doesn't contain
`{{ ` or `{% ` keeps the legacy interpolate behaviour exactly.
Pinning that here so future Jinja-feature additions don't
accidentally break existing prompts.
"""
from __future__ import annotations

import time

import pytest

from orchestra.budget import BudgetState
from orchestra.runcontext import RunContext
from orchestra.template_engine import (
    render,
    render_named,
    render_named_kwargs,
)


def _state(**kw) -> BudgetState:
    defaults = dict(
        original_tokens=80_000,
        original_steps=127,
        original_max_context=16_000,
        original_wall_time=1200,
        tokens_in=5_700,
        cumulative_paid=1_000,
        steps_used=2,
        wall_start=time.time() - 5,
    )
    defaults.update(kw)
    return BudgetState(**defaults)


# ── render() auto-detect ───────────────────────────────────────────


class TestAutoDetect:

    def test_legacy_braces_use_interpolate(self):
        ctx = RunContext(data={"pr_title": "ORD-301: Add credit"})
        out = render("PR: {pr_title}", ctx)
        assert out == "PR: ORD-301: Add credit"

    def test_jinja_double_braces_use_jinja(self):
        ctx = RunContext(data={"pr_title": "ORD-301: Add credit"})
        out = render("PR: {{ pr_title }}", ctx)
        assert out == "PR: ORD-301: Add credit"

    def test_jinja_block_tag_routes_to_jinja(self):
        ctx = RunContext(data={"items": ["a", "b", "c"]})
        out = render(
            "{% for it in items %}- {{ it }}\n{% endfor %}", ctx,
        )
        assert "- a" in out and "- b" in out and "- c" in out

    def test_unknown_legacy_key_left_literal(self):
        """Legacy interpolate leaves `{not_a_var}` as-is — same
        behaviour as before the engine introduction. Pinning this
        so JSON-in-prompts examples don't accidentally interpolate."""
        ctx = RunContext(data={"a": 1})
        out = render("config = {nonexistent}", ctx)
        assert out == "config = {nonexistent}"

    def test_jinja_undefined_collapses_empty(self):
        """RunContext uses ChainableUndefined — missing vars in
        Jinja templates render as empty rather than raising. Safer
        for opt-in placeholders like `{{ skills }}` that may or
        may not be populated for a given run."""
        ctx = RunContext()
        out = render("before {{ nothing }} after", ctx)
        # ChainableUndefined renders as empty string.
        assert out == "before  after"

    def test_empty_text_returns_empty(self):
        ctx = RunContext()
        assert render("", ctx) == ""


# ── RunContext.to_kwargs ───────────────────────────────────────────


class TestRunContext:

    def test_data_field_visible(self):
        ctx = RunContext(data={"pr_title": "x"})
        kw = ctx.to_kwargs()
        assert kw["pr_title"] == "x"

    def test_framework_helpers_present(self):
        ctx = RunContext(data={})
        kw = ctx.to_kwargs()
        # All standard names exist even when state is absent.
        for name in ("skills", "budget_stats", "budget_stats_legend",
                     "time_info", "context_pct", "steps_used",
                     "agent_name", "depth"):
            assert name in kw

    def test_skills_property_pulls_from_skills_body(self):
        ctx = RunContext(skills_body="## Skill: foo\n\nbody.")
        kw = ctx.to_kwargs()
        assert kw["skills"] == "## Skill: foo\n\nbody."

    def test_context_pct_with_budget_state(self):
        ctx = RunContext(budget_state=_state(tokens_in=8_000,
                                              original_max_context=16_000))
        assert ctx.context_pct == 50

    def test_context_pct_zero_without_state(self):
        ctx = RunContext()
        assert ctx.context_pct == 0

    def test_budget_stats_renders_when_state_present(self):
        ctx = RunContext(budget_state=_state())
        out = ctx.budget_stats
        # Compact-table layout markers.
        assert "own ctx" in out
        assert "spawn:" in out

    def test_budget_stats_empty_without_state(self):
        ctx = RunContext()
        assert ctx.budget_stats == ""

    def test_framework_helpers_dont_shadow_data_in_to_kwargs(self):
        """Conflict resolution: framework names win over data names
        of the same key. This means a prompt can rely on
        `{{ skills }}` always meaning the mounted skill bodies,
        even if some upstream caller accidentally injects a data
        field named `skills`."""
        ctx = RunContext(
            data={"skills": "WRONG"},
            skills_body="RIGHT",
        )
        kw = ctx.to_kwargs()
        assert kw["skills"] == "RIGHT"

    def test_getitem_mapping_shim(self):
        ctx = RunContext(data={"x": 1}, skills_body="s")
        assert ctx["x"] == 1
        assert ctx["skills"] == "s"
        with pytest.raises(KeyError):
            ctx["nope"]


# ── render_named (full RunContext) ─────────────────────────────────


class TestRenderNamed:

    def test_renders_with_state_template(self):
        ctx = RunContext(budget_state=_state())
        out = render_named("with_state", ctx)
        # Composed: literal lead + time_info + budget_stats snapshot.
        assert "Reflection noted." in out
        assert "Now:" in out
        assert "own ctx" in out


# ── render_named_kwargs (narrow dict) ──────────────────────────────


class TestRenderNamedKwargs:

    def test_lets_caller_control_namespace_fully(self):
        """budget_stats template uses `steps_used` as a narrow
        meaning ("agent's own step counter") — must not be
        shadowed by RunContext's framework helper. Verified by
        passing kwargs directly with a non-zero value while
        RunContext's property would otherwise return 0 (no
        state)."""
        out = render_named_kwargs(
            "budget_stats",
            tokens_in="8K", max_context="20K",
            own_bar="▰▰▰▰▱▱▱▱▱▱", pct=40,
            paid="3K", max_tokens="50K",
            shared_bar="▱▱▱▱▱▱▱▱▱▱", shared_pct=6,
            steps_used=3, max_steps=10,
            elapsed="5s", wall_max="20m",
            wall_bar="▱▱▱▱▱▱▱▱▱▱", wall_pct=0,
            spawn_carved="20-30K", spawn_carved_steps="10-20",
            spawn_return="3-5K",
            children=[],
        )
        # The narrow steps_used reached the template uncorrupted.
        assert "(steps 3/10)" in out
