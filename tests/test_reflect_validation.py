"""Reflect goes through `registry.dispatch` like every other tool.

A malformed reflect (e.g. qwen3-6 stuffing JSON into a single
`learned` string, which makes the JSON parser produce
`{"learned": "...everything..."}` with the other required fields
silently missing) must therefore trip JSON-Schema validation, and
the model must see the resulting error string as the reflect's
tool_result. That feedback loop is what breaks the reflect-spam
degenerate runs.

Before consolidating, reflect was handled inline in the agent loop:
`sgr.record(step, args)` + AGENT_REFLECT emit + counter reset all
ran BEFORE registry.dispatch's `_validate_args`. So a malformed
reflect was treated as success at every layer except the actual
result string. The cadence counter reset, the model got
"Reflection noted.", and it kept looping.
"""
from __future__ import annotations

import pytest

from orchestra import ToolRegistry
from orchestra.tools.builtin import register_builtins
from orchestra.types import AgentConfig, BudgetConfig
from orchestra.sgr import SGRTracker


def _setup() -> tuple[ToolRegistry, SGRTracker, AgentConfig]:
    """Minimal stand-in for the bits of an agent that `register_builtins`
    needs: a tool registry + an SGRTracker + an AgentConfig with
    `reflect` in its tools list. No real agent loop is started — we
    only exercise the dispatch path."""
    config = AgentConfig(
        name="probe",
        tools=["reflect"],
        budget=BudgetConfig(max_tokens=1000, max_steps=10),
    )
    registry = ToolRegistry()
    sgr = SGRTracker(None)
    # `agent=None` makes the reflect handler's side-effect block a
    # no-op (no event_bus, no `_current_step`) — exactly what we want
    # in this unit test: we're asserting the validation result, not
    # the side effects.
    register_builtins(registry, config, sgr_tracker=sgr, agent=None)
    return registry, sgr, config


class TestReflectValidation:

    def test_well_formed_reflect_succeeds(self):
        """All required fields present — dispatch runs the handler
        and returns the success sentinel."""
        registry, _sgr, _ = _setup()
        result = registry.dispatch("reflect", {
            "learned": "Read the diff.",
            "questions_remaining": [{"id": "Q1", "text": "What about X?"}],
            "confidence": "medium",
            "next_action": "Read X.java to find out.",
        })
        assert result == "Reflection noted.", (
            f"well-formed reflect should succeed, got: {result!r}"
        )

    def test_malformed_reflect_only_learned_rejected(self):
        """The mediaplanner failure mode: model emits malformed JSON
        that the parser salvages as `{"learned": "..."}`, with
        `questions_remaining` / `confidence` / `next_action`
        silently swallowed into the string. Dispatch must return a
        validation error string so the LLM sees it as the
        tool_result."""
        registry, _sgr, _ = _setup()
        bad_args = {
            "learned": (
                'I have read all... NEEDS_WORK verdict.", '
                '"questions_remaining": [{"id": "Q1"}], '
                '"confidence": "high"'
            ),
            # NB: no `questions_remaining` / `confidence` / `next_action`
        }
        result = registry.dispatch("reflect", bad_args)
        assert isinstance(result, str)
        assert result.startswith("validation error"), (
            f"malformed reflect should fail validation; got: {result!r}"
        )
        # And the missing required field must be named in the error
        # so the LLM has a concrete pointer to fix.
        assert "questions_remaining" in result

    def test_missing_confidence_rejected(self):
        registry, _sgr, _ = _setup()
        result = registry.dispatch("reflect", {
            "learned": "X",
            "questions_remaining": [{"id": "Q1", "text": "?"}],
            "next_action": "do Y",
            # `confidence` missing
        })
        assert result.startswith("validation error")
        assert "confidence" in result

    def test_invalid_confidence_enum_rejected(self):
        """`confidence` must be one of low/medium/high — typos
        surface at validation."""
        registry, _sgr, _ = _setup()
        result = registry.dispatch("reflect", {
            "learned": "X",
            "questions_remaining": [{"id": "Q1", "text": "?"}],
            "confidence": "very high",  # not in the enum
            "next_action": "do Y",
        })
        assert result.startswith("validation error")

    def test_malformed_question_item_rejected(self):
        """Items in `questions_remaining` need `id` + `text`. Missing
        either trips nested-schema validation."""
        registry, _sgr, _ = _setup()
        result = registry.dispatch("reflect", {
            "learned": "X",
            "questions_remaining": [{"id": "Q1"}],  # no 'text'
            "confidence": "high",
            "next_action": "do Y",
        })
        assert result.startswith("validation error")
        assert "text" in result

    def test_failed_reflect_does_not_record_to_sgr(self):
        """Side effects must not fire when validation rejected the
        call. SGRTracker.record runs inside the handler; if the
        handler ran for a malformed reflect, we'd see the bad data
        in `sgr.history` and the agent's reasoning trail would be
        corrupted."""
        registry, sgr, _ = _setup()
        bad_args = {"learned": "only learned, missing everything else"}
        result = registry.dispatch("reflect", bad_args)
        assert result.startswith("validation error")
        # Reflect handler never ran → SGR history untouched.
        assert sgr.history == [], (
            f"sgr.history should stay empty on validation failure; "
            f"got: {sgr.history!r}"
        )
