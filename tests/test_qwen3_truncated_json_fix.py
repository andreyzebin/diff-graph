"""Repair the second qwen3 tool-call failure mode: truncated JSON.

Sister fix to `test_qwen3_stringification_fix.py` (which handles the
"all args packed into one escaped string" shape). This one targets
the **emission-layer** failure: qwen3 sometimes emits a key with no
value before the closing brace, producing JSON that the standard
parser rejects outright. Observed live on plan 192 reviewer step
10 — all 6 `pr_post_comment` tool calls came back as `validation
error: 'text' is a required property` because every call ended in
`"parent_id": }` (a key, a colon, then `}` with no value between
them). The standard `json.loads` raises JSONDecodeError; the agent
loop's existing fallback turned `args` into `{}`; schema validation
then honestly reported the (now-empty) `text` as missing — but the
ROOT cause was JSON syntax, not a missing field.

`_repair_truncated_json` is intentionally narrow: it drops only
`"key": <empty>` pairs that have nothing between the colon and the
next structural token (comma or brace). Two regexes cover the
positional variants (leading / inner / trailing). The whole thing
runs under the existing `fix_qwen3_stringification_bug` provider
flag — they're two faces of the same qwen3 JSON-emission bug
family.

This test file pins:

  - The exact repair shape via the actual failing payload from the
    trace (one of the 6 pr_post_comment calls, copied verbatim from
    the run-data) — proves the fix recovers ALL of file/line/
    severity/text without damaging the long `text` content that
    contains commas, colons, backticks, and parentheses.
  - The positional variants and a battery of "must not touch this"
    cases — fully-valid JSON, strings that look like keys but
    aren't, nested objects.
  - The `_parse_tool_arguments` toggle behaviour — flag off means
    legacy `{}` fallback; flag on engages the repair.
"""
from __future__ import annotations

import json

import pytest

from orchestra.tools.registry import (
    _repair_truncated_json,
    _repair_over_escaped_keys,
    _parse_tool_arguments,
)


# ── _repair_truncated_json: pure string transform ──────────────────

# The actual broken JSON from plan 192 reviewer step 10, one of the
# six pr_post_comment calls. Pasted verbatim from
# /api/runs/604d9a278772/step/.../call?as=text so the test fails
# loudly if the repair ever stops recovering the real-world shape.
REAL_FAILING_ARGS = (
    '{"file": "src/main/java/com/flowmart/orders/controller/PromotionController.java",'
    ' "line": 38,'
    ' "severity": "MAJOR",'
    ' "text": "Missing ownership check — authorization bypass.\\n\\n'
    'OrderController checks `order.getCustomer().getEmail().equals(currentUser.getUsername())`'
    ' on every endpoint (lines 28, 40, 52). PromotionController.applyPromotion() retrieves'
    ' the order but never verifies that the authenticated user owns it. Any user can'
    ' apply a promotion to any order by guessing or enumerating order IDs.\\n\\n'
    'Fix: Add the same ownership check pattern used in OrderController before calling'
    ' pricingService.applyBulkDiscount().",'
    ' "parent_id": }'
)


class TestRepairRealFailingPayload:
    """The whole point of this fix: the real production failure
    parses cleanly after one repair pass, with NO data damage."""

    def test_real_payload_becomes_parseable(self):
        repaired = _repair_truncated_json(REAL_FAILING_ARGS)
        # Should be valid JSON now — no exception.
        parsed = json.loads(repaired)
        assert isinstance(parsed, dict)

    def test_real_payload_preserves_every_other_field(self):
        """The whole reason we can't just drop the pr_post_comment call
        is that `file`, `line`, `severity`, and `text` were all
        correct — only `parent_id` was malformed. Verify each
        survives the repair byte-for-byte."""
        parsed = json.loads(_repair_truncated_json(REAL_FAILING_ARGS))
        assert parsed["file"] == (
            "src/main/java/com/flowmart/orders/controller/PromotionController.java"
        )
        assert parsed["line"] == 38
        assert parsed["severity"] == "MAJOR"
        # The text body — long, contains commas, colons, backticks,
        # parentheses, newlines — must round-trip unchanged. Spot
        # check several substrings that would break if the regex
        # accidentally matched something inside the string literal.
        text = parsed["text"]
        assert "authorization bypass" in text
        assert "(lines 28, 40, 52)" in text                # commas inside parens
        assert "`order.getCustomer().getEmail()" in text   # backticks + dots + parens
        assert "Fix: Add the same ownership check" in text # colon inside string

    def test_real_payload_drops_only_parent_id(self):
        """Belt-and-braces: parent_id was the only truncated key and
        must be the ONLY one missing after repair. Every other key
        from the original is still present."""
        parsed = json.loads(_repair_truncated_json(REAL_FAILING_ARGS))
        assert set(parsed.keys()) == {"file", "line", "severity", "text"}
        assert "parent_id" not in parsed


class TestRepairPositionalVariants:
    """Three structural positions where an empty value can appear.
    Each must be repaired without affecting siblings."""

    def test_trailing_empty_pair_before_close_brace(self):
        """The qwen3 wild-type: `"k": }`. Comma-prefixed empty key
        right before the closing brace."""
        assert json.loads(_repair_truncated_json('{"a": 1, "b": }')) == {"a": 1}

    def test_inner_empty_pair_between_keys(self):
        """`"k": ,` between two valid keys. Repair drops the empty
        pair, the valid ones survive."""
        out = json.loads(_repair_truncated_json('{"a": 1, "b": , "c": 2}'))
        assert out == {"a": 1, "c": 2}

    def test_leading_empty_pair_followed_by_valid(self):
        """First key in the object is truncated. No comma before it
        — must still be removed without leaving a stray comma."""
        assert json.loads(_repair_truncated_json('{"a": , "b": 2}')) == {"b": 2}

    def test_leading_and_only_empty_pair(self):
        """`{"a": }` — single truncated key, no siblings. Repairs
        to an empty object."""
        assert json.loads(_repair_truncated_json('{"a": }')) == {}

    def test_multiple_truncated_keys_one_pass(self):
        """Several truncated pairs in one payload — the repair runs
        until a fixed point, so all of them get dropped."""
        out = json.loads(_repair_truncated_json(
            '{"x": 1, "a": , "b": , "y": "z", "c": }'
        ))
        assert out == {"x": 1, "y": "z"}

    def test_nested_object_with_truncated_inner_key(self):
        """The inner object is malformed; the outer one isn't.
        Repair must not damage the outer structure."""
        out = json.loads(_repair_truncated_json('{"outer": {"inner": }}'))
        assert out == {"outer": {}}


class TestRepairDoesNotTouchValidInput:
    """Inverse contract: anything that parses on its own must come
    out byte-identical (no false-positive matches that mangle
    correct payloads)."""

    @pytest.mark.parametrize("payload", [
        '{"text": "hello"}',
        '{"a": 1, "b": 2, "c": 3}',
        '{}',
        '[]',
        '{"nested": {"a": 1, "b": [1, 2, 3]}}',
        # Empty STRING values aren't truncated keys — the colon is
        # followed by `""`, not `,` or `}`.
        '{"text": "", "x": 1}',
        # Numbers / nulls / booleans are real values too.
        '{"a": 0, "b": null, "c": false, "d": true}',
    ])
    def test_valid_input_unchanged(self, payload):
        assert _repair_truncated_json(payload) == payload
        # Bonus: parsed output stays semantically identical.
        assert json.loads(_repair_truncated_json(payload)) == json.loads(payload)

    def test_string_with_comma_colon_brace_inside(self):
        """Strings can contain `, "fake_key":` byte sequences. The
        repair must not see them as truncated pairs because the
        outer quote context is wrong (regex requires the key be
        followed by `:` and then `,` or `}` at the structural
        level, which won't match inside string content)."""
        payload = '{"text": "before, \\"fake_key\\": after"}'
        # Round-trips: same string out, same content after parse.
        out = _repair_truncated_json(payload)
        assert json.loads(out) == {"text": 'before, "fake_key": after'}

    def test_text_with_lines_and_punctuation(self):
        """Specifically the kind of text body the reviewer produces
        — multi-paragraph, code-fenced, with markdown-like
        backticks. Must round-trip clean."""
        body = (
            "selectFreeItem returns `group.get(0)` (line 99). "
            "The acceptance criteria says \\\"the cheapest item is free\\\", "
            "but there is no sorting by price. Fix: use "
            "`group.stream().min(Comparator.comparing(OrderItem::getUnitPrice))`."
        )
        payload = f'{{"severity": "MAJOR", "text": "{body}"}}'
        out = _repair_truncated_json(payload)
        assert json.loads(out)["severity"] == "MAJOR"
        # Reconstruct what JSON decoded the body to and compare:
        assert json.loads(out)["text"] == json.loads(f'"{body}"')


class TestRepairDefensive:
    """Defensive — never crash on weird inputs."""

    def test_empty_string(self):
        assert _repair_truncated_json("") == ""

    def test_non_string_input(self):
        assert _repair_truncated_json(None) is None
        assert _repair_truncated_json(42) == 42

    def test_completely_malformed_unrelated(self):
        """Repair only handles its specific failure shape. If the
        input is broken in some OTHER way (unclosed string, missing
        brace, …) we should leave it for the caller to handle —
        returning something nominally-changed but still unparseable
        is worse than returning unchanged."""
        broken = '{"text": "unclosed'
        out = _repair_truncated_json(broken)
        # No truncated empty-pair pattern → no change.
        assert out == broken


# ── _parse_tool_arguments: integration with the dispatch path ──────

class TestParseToolArguments:
    """The function `agent.py` calls per tool_call. Tests both the
    flag-off legacy behaviour and the flag-on repair."""

    def test_valid_json_parses_unchanged(self):
        out = _parse_tool_arguments('{"text": "hi", "line": 7}', fix_qwen3=False)
        assert out == {"text": "hi", "line": 7}

    def test_flag_off_malformed_falls_back_to_empty(self):
        """Legacy contract preserved — when the flag is off we
        return `{}` on parse failure exactly like the original
        inline `try/except` did. Schema validation downstream will
        then report whatever's missing."""
        out = _parse_tool_arguments(REAL_FAILING_ARGS, fix_qwen3=False)
        assert out == {}

    def test_flag_on_real_payload_recovers_other_fields(self):
        """With the repair engaged, the four well-formed fields
        come through and `parent_id` is dropped."""
        out = _parse_tool_arguments(REAL_FAILING_ARGS, fix_qwen3=True)
        assert out["file"].endswith("PromotionController.java")
        assert out["line"] == 38
        assert out["severity"] == "MAJOR"
        assert "authorization bypass" in out["text"]
        assert "parent_id" not in out

    def test_flag_on_unrecoverable_still_empty(self):
        """The flag widens what we accept but doesn't break the
        fallback. A truly unparseable string (not just truncated
        pairs) still returns `{}`."""
        out = _parse_tool_arguments('{"text": "no closing quote', fix_qwen3=True)
        assert out == {}

    def test_array_at_root_coerced_to_empty(self):
        """The tool protocol expects an object at root — arrays are
        ill-formed and the dispatcher calls `handler(**args)` which
        would explode on a list. Coerce to `{}` so the validator
        gets a useful error instead."""
        out = _parse_tool_arguments('[1, 2, 3]', fix_qwen3=False)
        assert out == {}

    def test_none_input_treated_as_empty(self):
        """OpenAI's tool_call.arguments is sometimes None for
        argument-less tools — same fallback path as inline did."""
        out = _parse_tool_arguments(None, fix_qwen3=False)
        assert out == {}
        out2 = _parse_tool_arguments(None, fix_qwen3=True)
        assert out2 == {}


# ── Third shape: over-escaped top-level keys ───────────────────────


# Real payload from plan 218 run cbb7a22ec16d step 6 — a `reflect`
# call where qwen3 wrote every top-level object key with
# backslash-escaped quotes. JSON parser sees `\"X\":` as an escaped
# quote (literal `"` inside the string value), so the FIRST value
# string never closes → "Unterminated string starting at 12". The
# whole reflect payload becomes one giant unterminated `learned`
# value; agent-loop fallback turns args into `{}`, validation
# reports `'learned' is a required property`.
#
# This constant captures the EXACT failure shape for regression. A
# fix that stops working on this regresses the most common qwen3
# reflect failure mode.
REAL_OVER_ESCAPED_REFLECT = (
    '{"learned": "I read the diff. Key finding: selectFreeItem '
    'returns group.get(0) without sorting — BLOCKER.\\", '
    '\\"questions_remaining\\": [{\\"id\\": \\"Q1\\", '
    '\\"text\\": \\"Is ownership checked?\\"}], '
    '\\"confidence\\": \\"high\\", '
    '\\"next_action\\": \\"Submit findings now.\\"}'
)


class TestRepairOverEscapedKeys:
    """Pure string transform: un-escape one layer of quote-escaping
    when the input's top-level object keys were emitted as
    `\\"X\\":` instead of `"X":`."""

    def test_real_payload_becomes_parseable(self):
        """The exact qwen3 reflect-emit failure must parse cleanly
        after one un-escape pass."""
        repaired = _repair_over_escaped_keys(REAL_OVER_ESCAPED_REFLECT)
        parsed = json.loads(repaired)
        assert isinstance(parsed, dict)

    def test_real_payload_recovers_all_top_level_keys(self):
        """Every reflect field is restored to the top level — none
        get accidentally stranded inside a string."""
        parsed = json.loads(_repair_over_escaped_keys(REAL_OVER_ESCAPED_REFLECT))
        assert set(parsed.keys()) == {
            "learned", "questions_remaining", "confidence", "next_action",
        }
        assert parsed["confidence"] == "high"
        assert parsed["next_action"] == "Submit findings now."
        # questions_remaining is a list of dicts — the over-escape
        # also got applied inside the array literal and our un-escape
        # has to recover the nested dicts too.
        assert len(parsed["questions_remaining"]) == 1
        assert parsed["questions_remaining"][0]["id"] == "Q1"

    def test_no_escape_in_input_is_no_op(self):
        """Cheap-path reject: input that doesn't contain `\\"` at
        all must come out byte-identical (no work, no surprises)."""
        clean = '{"text": "hello", "n": 7}'
        assert _repair_over_escaped_keys(clean) == clean

    def test_non_string_input_safe(self):
        """Defensive: None / int / etc. just pass through."""
        assert _repair_over_escaped_keys(None) is None
        assert _repair_over_escaped_keys("") == ""
        assert _repair_over_escaped_keys(42) == 42

    def test_truncated_key_payload_not_changed_by_unescape(self):
        """The truncated-key shape (`"parent_id": }`) doesn't contain
        `\\"` sequences. The over-escape repair must be a no-op on
        it so the chain still routes it to the right handler.
        Cross-shape isolation test."""
        truncated = '{"text": "hi", "parent_id": }'
        assert _repair_over_escaped_keys(truncated) == truncated


class TestParseToolArgumentsCoversThirdShape:
    """`_parse_tool_arguments(fix_qwen3=True)` now cascades through
    both truncated-json AND over-escaped repairs; either one's
    success commits the result."""

    def test_over_escaped_recovers_via_parse_helper(self):
        out = _parse_tool_arguments(REAL_OVER_ESCAPED_REFLECT, fix_qwen3=True)
        assert "learned" in out
        assert out["confidence"] == "high"

    def test_over_escaped_flag_off_falls_through(self):
        """Without the flag the helper preserves legacy `return {}`
        behaviour for any unparseable input, regardless of shape."""
        out = _parse_tool_arguments(REAL_OVER_ESCAPED_REFLECT, fix_qwen3=False)
        assert out == {}

    def test_truncated_still_recovered(self):
        """Regression: the previously-fixed truncated-key shape
        must still pass through the cascading repair chain.
        Sanity that adding the new handler didn't break the old."""
        truncated = (
            '{"file": "X.java", "line": 10, "severity": "MAJOR", '
            '"text": "issue", "parent_id": }'
        )
        out = _parse_tool_arguments(truncated, fix_qwen3=True)
        assert out["text"] == "issue"
        assert "parent_id" not in out


class TestOverEscapedKeysHandlerInChain:
    """End-to-end via the registry's default qwen3 chain. Verifies
    the handler is registered, fires on the right precondition, and
    a committed candidate re-validates against the schema."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "learned": {"type": "string"},
            "questions_remaining": {"type": "array"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_action": {"type": "string"},
        },
        "required": ["learned", "confidence", "next_action"],
    }

    def _register(self, reg):
        from orchestra.tools.registry import ToolDef
        reg.register_tool_def(ToolDef(
            name="reflect_probe",
            description="reflect-shaped probe",
            parameters=self.SCHEMA,
            handler=lambda **kw: {"got": kw},
        ))

    def test_default_chain_includes_over_escaped_handler(self):
        """The default qwen3 chain ships four handlers in documented
        order. Order is part of the contract — narrower repairs
        first, broader ones after."""
        from orchestra.tools.arg_repair import default_qwen3_chain
        chain = default_qwen3_chain()
        names = [h.name for h in chain]
        assert names == ["truncated_json", "over_escaped_keys",
                          "python_literals", "stringified_args"]

    def test_dispatch_recovers_over_escaped_reflect(self):
        """End-to-end: dispatch a tool call whose raw arguments
        match the over-escape shape. The chain runs through
        truncated (no-op), over-escaped (recovers), stringified
        (skipped). Handler invoked with the recovered args."""
        from orchestra.tools.registry import ToolRegistry
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        # Pre-parse fails → args = {}. Pass raw to dispatch like
        # `agent.py` does after the inline `json.loads / except`
        # block.
        out = reg.dispatch("reflect_probe", {},
                            raw_args=REAL_OVER_ESCAPED_REFLECT)
        assert isinstance(out, dict)
        assert out["got"]["confidence"] == "high"
        assert "Submit findings" in out["got"]["next_action"]

    def test_dispatch_with_chain_off_returns_error(self):
        """Without the flag, no chain → the over-escaped payload is
        NOT recovered. Sanity that the fix is gated behind opt-in.

        The error surfaced is the honest JSON syntax error (the
        over-escape leaves the raw string unparseable), not the
        misleading `'learned' is a required property` — the
        honest-error check lives in `dispatch` itself, not the
        repair chain, so it fires even with no handlers."""
        from orchestra.tools.registry import ToolRegistry
        reg = ToolRegistry()  # no chain
        self._register(reg)
        out = reg.dispatch("reflect_probe", {},
                            raw_args=REAL_OVER_ESCAPED_REFLECT)
        assert isinstance(out, str)
        assert out.startswith("error: malformed JSON in arguments")
