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

`_repair_stringified_args` handles two sub-shapes (see its
docstring): the OBJECT shape (the string value is a complete escaped
JSON object) and the TAIL-FRAGMENT shape (the model started
stringifying at a property's value, so the string is
`<value>, "sib": ..., }` — recovered by re-attaching the property's
own key in front). `TestTailFragmentSubShape` below pins the second.
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


# ── Tail-fragment sub-shape ─────────────────────────────────────────

# The actual `questions_remaining` value from the trace: the model
# started stringifying at this property's VALUE, so the string holds
# the array it meant to emit, then its sibling keys, then the object's
# closing brace — `<value>, "confidence": ..., "next_action": ...}`.
# Leading/trailing `\n\n` is exactly what the model emitted. Faithful
# so the test fails loudly if the repair stops recovering it.
REAL_TAIL_FRAGMENT_VALUE = (
    "\n\n"
    '[{"id": "Q1", "text": "Does the implementation verify that the credit '
    'belongs to the same customer as the order being redeemed against?"}, '
    '{"id": "Q2", "text": "Does the implementation recalculate tax on the '
    'reduced subtotal using PricingService, or just subtract from the total?"}, '
    '{"id": "Q3", "text": "Is the \'apply at most once\' guard '
    'concurrency-safe with database-level or optimistic locking?"}, '
    '{"id": "Q4", "text": "Does the code check for expired credits?"}, '
    '{"id": "Q5", "text": "If credit > subtotal, does the remainder stay '
    'on balance?"}, '
    '{"id": "Q6", "text": "Is PricingService the single source of truth '
    'for tax per AGENTS.md?"}], '
    '"confidence": "high", '
    '"next_action": "Read AGENTS.md to confirm conventions, then submit '
    'findings via text_answer."}'
    "\n\n"
)

# After json.loads of the raw arguments — a well-formed 2-key object;
# `confidence` / `next_action` are trapped inside the second value.
REAL_TAIL_FRAGMENT_ARGS = {
    "learned": "Analyzed all the code. AC1 ownership check missing, AC2 tax "
               "not recalculated, AC3 concurrency unsafe, AC5 partial "
               "consumption wrong. Need to check AGENTS.md then submit.",
    "questions_remaining": REAL_TAIL_FRAGMENT_VALUE,
}

# reflect with `questions_remaining` as a real array property.
TAIL_FRAGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "learned": {"type": "string"},
        "questions_remaining": {"type": "array"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "next_action": {"type": "string"},
    },
    "required": ["learned", "questions_remaining", "confidence", "next_action"],
}


class TestTailFragmentSubShape:
    """`_repair_stringified_args` second sub-shape: the stringified
    value isn't a `{...}` object but a `<value>, "sib": ...}` fragment
    — recovered by re-attaching the property's own key."""

    def test_real_tail_fragment_gets_lifted(self):
        """The exact trace payload: `confidence` / `next_action` are
        trapped after the `questions_remaining` array inside one
        string. Re-prefixing the key reconstructs the object."""
        repaired = _repair_stringified_args(
            REAL_TAIL_FRAGMENT_ARGS, TAIL_FRAGMENT_SCHEMA)
        assert repaired is not None, "tail-fragment shape must be recognised"
        assert repaired["confidence"] == "high"
        assert repaired["next_action"].startswith("Read AGENTS.md")
        # `learned` was a clean top-level key — untouched.
        assert repaired["learned"] == REAL_TAIL_FRAGMENT_ARGS["learned"]

    def test_questions_remaining_recovered_as_array(self):
        """Bonus over the object sub-shape: the property's own value is
        recovered with its real type. `questions_remaining` comes back
        as a list of dicts, not the original trapped string."""
        repaired = _repair_stringified_args(
            REAL_TAIL_FRAGMENT_ARGS, TAIL_FRAGMENT_SCHEMA)
        qr = repaired["questions_remaining"]
        assert isinstance(qr, list)
        assert len(qr) == 6
        assert qr[0] == {"id": "Q1", "text": qr[0]["text"]}
        assert qr[2]["text"].startswith("Is the 'apply at most once'")

    def test_tail_fragment_re_validates_against_schema(self):
        """End-to-end via the registry: the recovered args pass schema
        validation and the handler fires with the real kwargs."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        from orchestra.tools.registry import ToolDef
        reg.register_tool_def(ToolDef(
            name="reflect_q", description="reflect with questions",
            parameters=TAIL_FRAGMENT_SCHEMA,
            handler=lambda **kw: {"got": kw},
        ))
        result = reg.dispatch("reflect_q", REAL_TAIL_FRAGMENT_ARGS)
        assert isinstance(result, dict), f"expected handler to fire; got {result!r}"
        assert result["got"]["confidence"] == "high"
        assert len(result["got"]["questions_remaining"]) == 6

    def test_partial_tail_fragment_rejected(self):
        """Same ALL-or-nothing contract as the object sub-shape: if the
        fragment doesn't cover every missing-required key, no lift."""
        args = {
            "learned": "ok",
            # fragment carries confidence but NOT next_action
            "questions_remaining": '[{"id": "Q1"}], "confidence": "high"}',
        }
        assert _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA) is None

    def test_value_not_ending_with_brace_skipped(self):
        """The cheap pre-check: a normal `learned` body ends with prose,
        not `}`. It can't be either sub-shape — skipped before any
        parse attempt."""
        args = {
            "learned": "Analyzed the code, found a BLOCKER in PricingService.",
            "questions_remaining": "still just plain prose, no braces here",
        }
        assert _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA) is None

    def test_object_sub_shape_still_works(self):
        """Regression: adding the tail-fragment branch didn't break the
        original object sub-shape (`{...}` value)."""
        inner = {"learned": "x", "questions_remaining": [],
                 "confidence": "low", "next_action": "stop"}
        args = {"learned": json.dumps(inner)}
        repaired = _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA)
        assert repaired is not None
        assert repaired["confidence"] == "low"
        assert repaired["next_action"] == "stop"


# ── String-value tail-fragment (third sub-shape) ────────────────────

# The actual `learned` value from a reviewer trace where the model
# emitted prose for `learned` and failed to escape the closing `"` —
# the sibling fields got swallowed into the string. json.loads of the
# outer args succeeded as a 1-key dict; that 1 string's PYTHON form
# is what's pinned here. Faithful so the test fails loudly if the
# repair stops recovering this real-world shape.
#
# Note the embedded `"`: these are literal quote characters inside
# the Python string — the model intended them as JSON syntax for the
# sibling keys but they ended up as content. The repair re-wraps the
# whole thing with a leading `"`, turning the first internal `"`
# (right after "matches.") into the real string terminator.
REAL_STRING_TAIL_FRAGMENT = (
    "- The PR fixes a hanging business process by adding a default "
    "sequence flow from ExclusiveGateway3 to IntermediateCatchSignal2.\n"
    "- The fix is correct and addresses the ticket's acceptance "
    "criteria.\n"
    "- No PR threads exist, so this is a fresh review.\n"
    "- I've posted a COMMENT-level finding confirming the fix is "
    'correct.", "confidence": "high", "next_action": "Set review '
    'status to APPROVED and finish with findings"}'
)

REAL_STRING_TAIL_FRAGMENT_ARGS = {
    "learned": REAL_STRING_TAIL_FRAGMENT,
}


class TestStringValueTailFragmentSubShape:
    """Third sub-shape of `_repair_stringified_args`: tail-fragment
    where the property's real value was a STRING (prose), not a JSON
    token. Strategy 2 (re-attach key, parse value as token) yields
    invalid JSON because prose isn't a valid token after `:`. Strategy
    3 wraps the trapped content in a leading `"` — the first internal
    `"` then becomes the legitimate close and the rest parses as
    siblings."""

    # The real reviewer reflect schema requires only learned /
    # confidence / next_action — questions_remaining is optional. The
    # trace payload mirrors that (`questions_remaining` absent), so
    # this sub-class uses the same 3-required schema as
    # `TestRepairStringifiedArgs` rather than `TAIL_FRAGMENT_SCHEMA`
    # (which requires `questions_remaining` too).
    SCHEMA = {
        "type": "object",
        "properties": {
            "learned": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_action": {"type": "string"},
        },
        "required": ["learned", "confidence", "next_action"],
    }

    def test_real_string_value_payload_gets_lifted(self):
        """The exact reviewer trace: `learned` carries prose plus
        trapped `confidence` and `next_action` as escaped sibling
        keys. After repair both required siblings come out as
        proper top-level keys."""
        repaired = _repair_stringified_args(
            REAL_STRING_TAIL_FRAGMENT_ARGS, self.SCHEMA)
        assert repaired is not None, (
            "string-value tail-fragment must be recognised"
        )
        assert repaired["confidence"] == "high"
        assert repaired["next_action"].startswith("Set review status")

    def test_real_string_value_payload_recovers_learned_as_string(self):
        """Bonus over the OBJECT sub-shape: the property's own value
        comes back as a clean string ending at the model's intended
        terminator (right after "correct.")."""
        repaired = _repair_stringified_args(
            REAL_STRING_TAIL_FRAGMENT_ARGS, self.SCHEMA)
        learned = repaired["learned"]
        assert isinstance(learned, str)
        assert learned.startswith("- The PR fixes")
        assert learned.endswith("correct.")
        # The sibling keys are NOT inside the recovered `learned` —
        # they were lifted to the top level.
        assert "confidence" not in learned
        assert "next_action" not in learned

    def test_synthetic_minimal_string_value(self):
        """Minimal repro to cover the path without the verbose real
        payload: trapped value = `<prose>", "<sib>": "<v>"}`."""
        args = {
            "learned": 'short prose.", "confidence": "medium", '
                       '"next_action": "go"}',
        }
        repaired = _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA)
        # questions_remaining is required by TAIL_FRAGMENT_SCHEMA but
        # missing from both args and the lifted dict — repair MUST
        # refuse (must cover ALL missing-required, see existing
        # `test_partial_lift_is_rejected` contract).
        assert repaired is None

    def test_synthetic_minimal_string_value_covers_all_required(self):
        """Same as above but the trapped fragment covers EVERY
        missing-required key — repair commits."""
        args = {
            "learned": 'short prose.", "questions_remaining": [], '
                       '"confidence": "medium", "next_action": "go"}',
        }
        repaired = _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA)
        assert repaired is not None
        assert repaired["learned"] == "short prose."
        assert repaired["questions_remaining"] == []
        assert repaired["confidence"] == "medium"
        assert repaired["next_action"] == "go"

    def test_string_value_partial_lift_rejected(self):
        """Same ALL-or-nothing contract: string-value reconstruction
        must not commit when the lifted dict doesn't cover every
        missing-required."""
        # Only confidence here; next_action missing.
        args = {
            "learned": 'prose.", "confidence": "high"}',
        }
        assert _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA) is None

    def test_token_value_sub_shape_still_works(self):
        """Cross-shape regression: adding the string-value candidate
        didn't break Strategy 2 (token-value reconstruction)."""
        args = {
            "learned": "ok",
            "questions_remaining": '[{"id": "Q1"}], "confidence": "high", '
                                    '"next_action": "go"}',
        }
        repaired = _repair_stringified_args(args, TAIL_FRAGMENT_SCHEMA)
        assert repaired is not None
        assert repaired["confidence"] == "high"
        assert isinstance(repaired["questions_remaining"], list)

    def test_dispatch_end_to_end_recovers_string_value(self):
        """End-to-end through the registry's default qwen3 chain.
        The handler fires with the lifted args; result is a dict,
        not a validation-error string."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        from orchestra.tools.registry import ToolDef
        reg.register_tool_def(ToolDef(
            name="reflect_str",
            description="reflect-shaped probe (string-value tail)",
            parameters=TAIL_FRAGMENT_SCHEMA,
            handler=lambda **kw: {"got": kw},
        ))
        # Need a payload covering every required key — real payload
        # is missing `questions_remaining`, so augment it for the
        # end-to-end dispatch test. Same shape, sibling set complete.
        args = {
            "learned": ('analysis done.", "questions_remaining": [], '
                        '"confidence": "high", "next_action": "submit"}'),
        }
        result = reg.dispatch("reflect_str", args)
        assert isinstance(result, dict), f"expected handler to fire; got {result!r}"
        assert result["got"]["confidence"] == "high"
        assert result["got"]["learned"] == "analysis done."
