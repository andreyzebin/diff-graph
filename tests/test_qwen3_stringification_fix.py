"""Recovery for the qwen3 tool-call stringification bug.

Qwen3-Coder (vLLM / modelrun deployments) periodically emits tool
arguments as one top-level string property whose value is an escaped
JSON object containing all the other fields it should have produced
at the top level. See:

  - https://github.com/QwenLM/qwen-code/issues/379
  - https://github.com/vllm-project/vllm/issues/21711
  - https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/14

Observed in our reviewer reflect-spam runs (plan 192, REV-U-004 on
qwen3-6 — 60 reflects in 128s, ~1M tokens, score 0/1): every reflect
call had `confidence` and `next_action` packed inside the `learned`
string instead of as sibling keys; the validator correctly rejected
each with `'confidence' is a required property`, but the model
couldn't break out of the pattern and kept re-emitting the same
shape until the token budget ran out.

When `ToolRegistry(fix_qwen3_stringification_bug=True)`, dispatch
tries one repair pass via `_repair_stringified_args` before
returning the validation error: if some string-typed property's
value parses as a JSON object whose keys cover every missing-required
key, those keys are lifted to the top level and the stringified
container is dropped. Off by default; opt-in per provider profile
(`fix_qwen3_stringification_bug = true` in `.llm_creds.toml`).
"""
from __future__ import annotations

import json

import pytest

from orchestra.tools.registry import (
    ToolRegistry,
    _repair_stringified_args,
)


# ── Pure helper unit tests ──────────────────────────────────────────

class TestRepairStringifiedArgs:
    """`_repair_stringified_args` in isolation — no registry involved."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "learned": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_action": {"type": "string"},
        },
        "required": ["learned", "confidence", "next_action"],
    }

    def test_canonical_qwen3_shape_gets_lifted(self):
        """The exact failure shape from plan 192 logs: `learned` carries
        an escaped JSON blob containing the other required keys."""
        inner = {
            "learned": "PricingService.java:95 returns group.get(0) (BLOCKER).",
            "confidence": "high",
            "next_action": "Post findings and set verdict.",
        }
        args = {"learned": json.dumps(inner)}

        repaired = _repair_stringified_args(args, self.SCHEMA)

        assert repaired is not None, "should recognise lift-able shape"
        assert repaired["confidence"] == "high"
        assert repaired["next_action"] == "Post findings and set verdict."
        # Lifted `learned` from the inner JSON overrides the outer
        # stringified wrapper — the inner value is what the model
        # actually intended to put there.
        assert repaired["learned"] == inner["learned"]

    def test_well_formed_args_return_none(self):
        """Args that already pass schema validation must not be touched
        — None signals 'no repair attempted'."""
        good = {
            "learned": "OK.",
            "confidence": "high",
            "next_action": "Done.",
        }
        assert _repair_stringified_args(good, self.SCHEMA) is None

    def test_no_string_property_has_lift_able_json(self):
        """Missing keys but no candidate string contains them — give up."""
        args = {"learned": "just plain text, not JSON at all"}
        assert _repair_stringified_args(args, self.SCHEMA) is None

    def test_partial_lift_is_rejected(self):
        """Repair only fires when ALL missing-required keys live in the
        candidate. Half a lift is worse than none — would mask a real
        validation failure as a partial-shape success."""
        inner = {"confidence": "high"}  # missing next_action
        args = {"learned": json.dumps(inner)}
        assert _repair_stringified_args(args, self.SCHEMA) is None

    def test_lifted_keys_overlay_top_level(self):
        """If the lifted JSON also carries the wrapper key, the lifted
        value wins. (Model's intent for any duplicate is the inner one;
        the outer wrapper is just packaging.)"""
        inner = {
            "learned": "inner-value",
            "confidence": "low",
            "next_action": "stop",
        }
        args = {"learned": json.dumps(inner), "extra": "kept"}
        repaired = _repair_stringified_args(args, self.SCHEMA)
        assert repaired["learned"] == "inner-value"
        # Non-wrapper top-level keys survive.
        assert repaired["extra"] == "kept"

    def test_non_object_json_is_ignored(self):
        """A string that parses as JSON but yields a list/scalar isn't
        a candidate — we only lift objects."""
        args = {"learned": json.dumps([1, 2, 3])}
        assert _repair_stringified_args(args, self.SCHEMA) is None

    def test_malformed_json_is_ignored(self):
        """Trailing-garbage JSON shouldn't crash the repair attempt."""
        args = {"learned": "{not valid json"}
        assert _repair_stringified_args(args, self.SCHEMA) is None

    def test_no_required_in_schema_no_repair(self):
        """A schema with no required keys can't have 'missing required'
        — repair has no work to do."""
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        args = {"x": json.dumps({"y": 1})}
        assert _repair_stringified_args(args, schema) is None

    def test_non_dict_args_or_schema_safe(self):
        """Defensive: never crash on weird inputs."""
        assert _repair_stringified_args(None, self.SCHEMA) is None
        assert _repair_stringified_args({}, None) is None


# ── Registry-level integration tests ───────────────────────────────

def _register_reflect_like_tool(reg: ToolRegistry) -> None:
    """Register a probe tool with the same schema shape as the real
    reflect — three required string fields, simple enum on one. Mirrors
    the failure surface we actually hit in production without dragging
    in the SGRTracker / AgentConfig wiring."""
    from orchestra.tools.registry import ToolDef
    reg.register_tool_def(ToolDef(
        name="reflect_probe",
        description="reflect-shaped probe",
        parameters={
            "type": "object",
            "properties": {
                "learned": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "next_action": {"type": "string"},
            },
            "required": ["learned", "confidence", "next_action"],
        },
        handler=lambda **kw: {"got": kw},
    ))


class TestDispatchRepairToggle:
    """The repair fires only when the registry was constructed with
    `fix_qwen3_stringification_bug=True`. Default behaviour is
    unchanged — important so non-qwen3 providers keep their old
    validation contract."""

    BAD_ARGS = {"learned": json.dumps({
        "learned": "real learned content",
        "confidence": "high",
        "next_action": "post findings",
    })}

    def test_default_off_returns_validation_error(self):
        reg = ToolRegistry()  # flag defaults to False
        _register_reflect_like_tool(reg)
        result = reg.dispatch("reflect_probe", self.BAD_ARGS)
        assert isinstance(result, str)
        assert "validation error" in result
        assert "confidence" in result

    def test_flag_on_recovers_and_calls_handler(self):
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_reflect_like_tool(reg)
        result = reg.dispatch("reflect_probe", self.BAD_ARGS)
        # Handler returned the lifted kwargs — not a validation-error string.
        assert isinstance(result, dict), (
            f"expected handler to fire; got {result!r}"
        )
        got = result["got"]
        assert got["confidence"] == "high"
        assert got["next_action"] == "post findings"
        assert got["learned"] == "real learned content"

    def test_flag_on_still_rejects_truly_invalid_args(self):
        """The toggle must not turn the validator off — only lift the
        specific qwen3 stringification shape. A plain missing key with
        no recoverable nested JSON still fails with the original
        validation message."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_reflect_like_tool(reg)
        result = reg.dispatch("reflect_probe", {"learned": "no confidence here"})
        assert isinstance(result, str)
        assert "validation error" in result

    def test_well_formed_args_unaffected_by_flag(self):
        """Sanity: valid args go through the same fast path regardless
        of the toggle. Repair doesn't even run on the happy path."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_reflect_like_tool(reg)
        result = reg.dispatch("reflect_probe", {
            "learned": "ok",
            "confidence": "low",
            "next_action": "stop",
        })
        assert isinstance(result, dict)
        assert result["got"]["confidence"] == "low"

    def test_clone_preserves_flag(self):
        """`registry.clone()` is how the agent loop materialises
        per-step views of the tool list (e.g. force-reflect narrowing).
        Losing the flag mid-loop would silently disable the fix half-
        way through a session."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_reflect_like_tool(reg)
        cloned = reg.clone()
        assert cloned.fix_qwen3_stringification_bug is True
        result = cloned.dispatch("reflect_probe", self.BAD_ARGS)
        assert isinstance(result, dict)
