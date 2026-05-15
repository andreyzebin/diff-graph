"""orchestra/messages.yaml is the single source of truth for budget
pusher / guard wordings. These tests verify:

- Every wording slot the code path actually uses resolves to a
  non-empty string at runtime.
- The catalog is consistent with the pusher classes that consume it
  (so renaming a key in the YAML or a class trips the test rather
  than silently falling back to the inline default forever).
- The lookup helper handles missing keys / wrong types defensively
  (default fallback is the load-bearing behaviour).
"""
from __future__ import annotations

import pytest

from orchestra.messages import msg


# ── Lookup helper contract ──────────────────────────────────────────

class TestMsgHelper:

    def test_known_key_returns_yaml_value(self):
        """A real key in the YAML resolves to its trimmed value
        (YAML `>-` folded scalars don't leak trailing whitespace)."""
        out = msg("budget.token.nudge", "FALLBACK")
        assert out != "FALLBACK"
        assert out.strip() == out

    def test_missing_top_level_falls_back(self):
        out = msg("does.not.exist", "fallback default")
        assert out == "fallback default"

    def test_missing_middle_segment_falls_back(self):
        out = msg("budget.does.not.exist", "fallback default")
        assert out == "fallback default"

    def test_default_empty_when_omitted(self):
        out = msg("does.not.exist")
        assert out == ""

    def test_non_string_leaf_falls_back(self):
        """If someone puts a dict / list at a leaf by mistake, the
        lookup returns the fallback — keeps callers safe from
        TypeError when they pass the result to `.format(...)`."""
        # `budget.token` is a dict, not a leaf string.
        out = msg("budget.token", "fallback")
        assert out == "fallback"


# ── Every consumed key resolves at runtime ──────────────────────────

class TestEveryConsumedKeyResolves:
    """The whole point of the catalog: no caller falls through to an
    inline default in production. If a key is missing from the YAML
    these tests fail loudly so the YAML is fixed (or the key is
    deleted from the code path it was meant to drive)."""

    @pytest.mark.parametrize("key", [
        "budget.token.nudge",
        "budget.token.force_done",
        "budget.wall_time.nudge",
        "budget.wall_time.force_done",
        "budget.steps.nudge",
        "budget.steps.force_done",
        "budget.context.nudge",
        "reflect.cadence.nudge",
    ])
    def test_key_resolves_to_non_empty(self, key):
        out = msg(key, "__MISSING__")
        assert out != "__MISSING__", f"missing key in messages.yaml: {key}"
        assert out, f"empty value in messages.yaml: {key}"


# ── Catalog and pusher class attrs agree ────────────────────────────

class TestPusherClassesUseCatalog:
    """The class-level DEFAULT_*_MSG attributes are populated from the
    catalog at import time. If the YAML changes but the class attrs
    don't reflect it, someone broke the wiring."""

    def test_token_pusher_messages_from_catalog(self):
        from orchestra.budget import TokenBudgetPusher
        assert TokenBudgetPusher.DEFAULT_NUDGE_MSG == msg("budget.token.nudge")
        assert TokenBudgetPusher.DEFAULT_FORCE_DONE_MSG == msg("budget.token.force_done")

    def test_time_pusher_messages_from_catalog(self):
        from orchestra.budget import TimeBudgetPusher
        assert TimeBudgetPusher.DEFAULT_NUDGE_MSG == msg("budget.wall_time.nudge")
        assert TimeBudgetPusher.DEFAULT_FORCE_DONE_MSG == msg("budget.wall_time.force_done")

    def test_step_pusher_messages_from_catalog(self):
        from orchestra.budget import StepBudgetPusher
        assert StepBudgetPusher.DEFAULT_NUDGE_MSG == msg("budget.steps.nudge")
        assert StepBudgetPusher.DEFAULT_FORCE_DONE_MSG == msg("budget.steps.force_done")

    def test_context_pusher_message_from_catalog(self):
        from orchestra.budget import ContextBudgetPusher
        assert ContextBudgetPusher.DEFAULT_NUDGE_MSG == msg("budget.context.nudge")


# ── Format placeholders are coherent ────────────────────────────────

class TestFormatPlaceholders:
    """Templates that take `{threshold}` must accept the substitution
    without crashing. Catches typos like `{tresh}` that would explode
    at apply time on a production run."""

    def test_reflect_cadence_template_formats(self):
        out = msg("reflect.cadence.nudge").format(threshold=3)
        assert "3" in out
        # No leftover `{...}` slot in the formatted result.
        assert "{" not in out and "}" not in out
