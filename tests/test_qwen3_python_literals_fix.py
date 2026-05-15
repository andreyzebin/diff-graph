"""Repair the fourth qwen3 tool-call failure mode: Python dict repr.

Sister fix to `test_qwen3_truncated_json_fix.py` (truncated `"key": }`
pairs) and the over-escaped-keys repair in that same file. This one
targets the **emission-layer** failure where the model emits a Python
dict *repr* instead of JSON: a bare `None` (also `True` / `False`,
single-quoted strings) where JSON wants `null`. One such literal is
enough to make `json.loads` reject the whole object — the parse falls
back to `{}` and schema validation then reports the FIRST required
key as missing, a misleading error since the model actually included
it.

Observed live on a reviewer step: four parallel `pr_post_comment` calls,
the only one carrying `parent_id` emitted it as `"parent_id": None`.
That call came back as `validation error: 'text' is a required
property` while its three siblings (no `parent_id`, valid JSON)
posted fine.

`_repair_python_literals` round-trips the raw string through
`ast.literal_eval` + `json.dumps` — Python's safe literal evaluator
accepts the whole Python-repr grammar, and the JSON re-serialise
guarantees the output is valid JSON (or the helper returns the input
unchanged). It runs under the existing `fix_qwen3_stringification_bug`
provider flag alongside the other three repairs.

This file also pins the **honest-error** behaviour: when a tool call's
raw arguments are unrecoverably malformed JSON, dispatch now surfaces
the JSON syntax error rather than the misleading schema
"required property" error.
"""
from __future__ import annotations

import json

import pytest

from orchestra.tools.registry import (
    _repair_python_literals,
    _repair_truncated_json,
    _repair_over_escaped_keys,
    _parse_tool_arguments,
    ToolRegistry,
    ToolDef,
)
from orchestra.tools.arg_repair import default_qwen3_chain


# ── The real failing payload ───────────────────────────────────────

# Copied verbatim from the trace: one of four parallel pr_post_comment
# calls, the only one with `parent_id` — emitted as the Python literal
# `None` instead of JSON `null`. Everything else in the payload is
# well-formed JSON; that single token makes `json.loads` reject it.
REAL_FAILING_ARGS = (
    '{"file": "src/main/java/com/flowmart/orders/controller/CustomerController.java",'
    ' "line": 46,'
    ' "severity": "BLOCKER",'
    ' "text": "**Authorization bypass: no ownership check on orderId.**\\n\\n'
    'The `redeemCredit` endpoint accepts an `orderId` from the request parameter'
    ' but never verifies that this order belongs to the authenticated customer.'
    ' Any authenticated user can redeem a store credit against any order in the'
    ' system.\\n\\nCompare with `OrderController.java` (lines 27-29, 39-41, 51-53)'
    ' which correctly checks'
    ' `order.getCustomer().getEmail().equals(currentUser.getUsername())` and throws'
    ' `AccessDeniedException` for unauthorized access.\\n\\n**Fix**: Fetch the order'
    " in the controller (or service), verify the order's customer email matches the"
    " authenticated user's username, and throw `AccessDeniedException` if it"
    ' doesn\'t.",'
    ' "parent_id": None}'
)


# ── _repair_python_literals: pure string transform ─────────────────

class TestRepairRealFailingPayload:
    """The real production failure parses cleanly after one repair
    pass, with NO data damage."""

    def test_real_payload_becomes_parseable(self):
        repaired = _repair_python_literals(REAL_FAILING_ARGS)
        parsed = json.loads(repaired)  # no exception
        assert isinstance(parsed, dict)

    def test_real_payload_preserves_every_field(self):
        """`file`, `line`, `severity`, `text` were all correct — only
        `parent_id` carried the bad literal. Every field survives,
        and unlike the truncated-key repair, `parent_id` is KEPT
        (recovered as JSON `null`, not dropped)."""
        parsed = json.loads(_repair_python_literals(REAL_FAILING_ARGS))
        assert parsed["file"].endswith("CustomerController.java")
        assert parsed["line"] == 46
        assert parsed["severity"] == "BLOCKER"
        assert parsed["parent_id"] is None
        text = parsed["text"]
        assert "Authorization bypass" in text
        assert "(lines 27-29, 39-41, 51-53)" in text         # commas in parens
        assert "`order.getCustomer().getEmail()" in text      # backticks + dots
        assert "**Fix**: Fetch the order" in text             # colon in string
        assert "doesn't" in text                              # apostrophe in string

    def test_real_payload_is_idempotent(self):
        """A second pass sees valid JSON — also a valid Python literal
        — and round-trips to itself (semantically)."""
        once = _repair_python_literals(REAL_FAILING_ARGS)
        twice = _repair_python_literals(once)
        assert json.loads(once) == json.loads(twice)


class TestRepairLiteralVariants:
    """`None` / `True` / `False` at assorted structural positions."""

    def test_none_trailing(self):
        assert json.loads(_repair_python_literals('{"a": 1, "b": None}')) \
            == {"a": 1, "b": None}

    def test_none_inner(self):
        assert json.loads(_repair_python_literals('{"a": None, "b": 2}')) \
            == {"a": None, "b": 2}

    def test_true_false(self):
        out = json.loads(_repair_python_literals('{"x": True, "y": False}'))
        assert out == {"x": True, "y": False}

    def test_none_nested(self):
        out = json.loads(_repair_python_literals('{"outer": {"inner": None}}'))
        assert out == {"outer": {"inner": None}}

    def test_none_in_array(self):
        out = json.loads(_repair_python_literals('{"xs": [1, None, 3]}'))
        assert out == {"xs": [1, None, 3]}

    def test_single_quoted_python_repr(self):
        """The broader Python-repr shape — single-quoted keys and
        values. `ast.literal_eval` handles it; `json.dumps` normalises
        to double quotes."""
        out = json.loads(_repair_python_literals("{'a': 'hi', 'b': None}"))
        assert out == {"a": "hi", "b": None}


class TestRepairDoesNotTouchValidInput:
    """Inverse contract: valid JSON comes out semantically identical.
    (Byte-identity isn't promised — a successful round-trip may
    re-spell whitespace — but `json.loads` of in and out must match,
    and in practice the helper only runs after `json.loads` already
    failed, so valid input never reaches it on the live path.)"""

    @pytest.mark.parametrize("payload", [
        '{"text": "hello"}',
        '{"a": 1, "b": 2, "c": 3}',
        '{"a": 0, "b": null, "c": false, "d": true}',
        '{"nested": {"a": 1, "b": [1, 2, 3]}}',
        '{"text": "the word None appears inside this string"}',
        '{"text": "True story, False alarm"}',
    ])
    def test_valid_input_semantically_unchanged(self, payload):
        out = _repair_python_literals(payload)
        assert json.loads(out) == json.loads(payload)

    def test_literal_word_inside_string_not_touched(self):
        """`None` as a substring of a string value must survive — the
        round-trip goes through real parsers, not a regex, so string
        content is never at risk."""
        payload = '{"text": "returns None when unset"}'
        assert json.loads(_repair_python_literals(payload)) \
            == {"text": "returns None when unset"}


class TestRepairDefensive:
    """Never crash; leave genuinely-unrelated breakage alone."""

    def test_empty_and_non_string(self):
        assert _repair_python_literals("") == ""
        assert _repair_python_literals("   ") == "   "
        assert _repair_python_literals(None) is None
        assert _repair_python_literals(42) == 42

    def test_unclosed_string_left_unchanged(self):
        """Not a Python-literal shape — `ast.literal_eval` raises,
        helper returns the input untouched so the next repair (or the
        `{}` fallback) handles it."""
        broken = '{"text": "unclosed'
        assert _repair_python_literals(broken) == broken

    def test_non_dict_literal_left_unchanged(self):
        """A bare list/scalar is a valid Python literal but not a tool
        args object — return unchanged so the dispatcher's
        coerce-to-`{}` contract still applies."""
        assert _repair_python_literals('[1, 2, 3]') == '[1, 2, 3]'
        assert _repair_python_literals('None') == 'None'

    def test_truncated_key_payload_not_changed(self):
        """Cross-shape isolation: the truncated-key shape
        (`"parent_id": }`) is not a valid Python literal either —
        `ast.literal_eval` raises, this repair is a no-op, and the
        chain routes it to `_repair_truncated_json`."""
        truncated = '{"text": "hi", "parent_id": }'
        assert _repair_python_literals(truncated) == truncated

    def test_over_escaped_payload_not_changed(self):
        """Cross-shape isolation with the over-escaped-keys shape."""
        over_escaped = '{"learned": "x\\", \\"confidence\\": \\"high\\"}'
        assert _repair_python_literals(over_escaped) == over_escaped


# ── _parse_tool_arguments: integration with the dispatch path ──────

class TestParseToolArguments:

    def test_flag_off_falls_back_to_empty(self):
        """Legacy contract — flag off, malformed input returns `{}`."""
        assert _parse_tool_arguments(REAL_FAILING_ARGS, fix_qwen3=False) == {}

    def test_flag_on_recovers_payload(self):
        out = _parse_tool_arguments(REAL_FAILING_ARGS, fix_qwen3=True)
        assert out["file"].endswith("CustomerController.java")
        assert out["line"] == 46
        assert out["severity"] == "BLOCKER"
        assert out["parent_id"] is None
        assert "Authorization bypass" in out["text"]

    def test_truncated_still_recovered(self):
        """Regression: adding the python-literals repair to the
        cascade didn't break the truncated-key shape."""
        truncated = (
            '{"file": "X.java", "line": 10, "severity": "MAJOR", '
            '"text": "issue", "parent_id": }'
        )
        out = _parse_tool_arguments(truncated, fix_qwen3=True)
        assert out["text"] == "issue"
        assert "parent_id" not in out

    def test_over_escaped_still_recovered(self):
        """Regression: the over-escaped-keys shape still recovers."""
        over_escaped = (
            '{"learned": "finding here.\\", '
            '\\"confidence\\": \\"high\\", '
            '\\"next_action\\": \\"submit\\"}'
        )
        out = _parse_tool_arguments(over_escaped, fix_qwen3=True)
        assert out["confidence"] == "high"


# ── PythonLiteralsHandler: end-to-end via the registry chain ───────

class TestPythonLiteralsHandlerInChain:

    SCHEMA = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "severity": {"type": "string"},
            "text": {"type": "string"},
            # parent_id intentionally unconstrained — the recovered
            # value is JSON `null`, which an `integer`-typed schema
            # would (correctly) reject. The live pr_post_comment schema
            # allows null; the probe mirrors that.
        },
        "required": ["text"],
    }

    def _register(self, reg, name="comment_probe"):
        reg.register_tool_def(ToolDef(
            name=name,
            description="pr_post_comment-shaped probe",
            parameters=self.SCHEMA,
            handler=lambda **kw: {"got": kw},
        ))

    def test_default_chain_includes_handler_in_order(self):
        names = [h.name for h in default_qwen3_chain()]
        assert names == ["truncated_json", "over_escaped_keys",
                          "python_literals", "stringified_args"]

    def test_dispatch_recovers_python_literal_payload(self):
        """End-to-end: the exact `parent_id: None` failure. Pre-parse
        fails → args = {}; dispatch gets the raw string like
        `agent.py` passes it; the chain runs truncated (no-op),
        over-escaped (no-op), python_literals (recovers)."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        out = reg.dispatch("comment_probe", {}, raw_args=REAL_FAILING_ARGS)
        assert isinstance(out, dict)
        assert out["got"]["severity"] == "BLOCKER"
        assert out["got"]["line"] == 46
        assert out["got"]["parent_id"] is None
        assert "Authorization bypass" in out["got"]["text"]

    def test_dispatch_with_chain_off_returns_error(self):
        """Without the flag there's no chain — but the honest-error
        path still fires (it lives in `dispatch`, not the chain), so
        the result names the JSON syntax problem, not a missing
        field."""
        reg = ToolRegistry()  # no chain
        self._register(reg)
        out = reg.dispatch("comment_probe", {}, raw_args=REAL_FAILING_ARGS)
        assert isinstance(out, str)
        assert "malformed JSON" in out


# ── Honest error: surface the syntax error, not the schema error ───

class TestHonestMalformedJsonError:
    """When raw arguments are unrecoverably malformed JSON, dispatch
    surfaces the JSON syntax error — not the misleading
    `'X' is a required property`. The model needs to fix its JSON,
    not re-supply a field it already sent."""

    SCHEMA = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def _register(self, reg):
        reg.register_tool_def(ToolDef(
            name="probe", description="probe",
            parameters=self.SCHEMA,
            handler=lambda **kw: {"got": kw},
        ))

    def test_unrecoverable_malformed_json_surfaces_syntax_error(self):
        """`{"text": "unclosed` — no repair handler recognises it.
        The result names the syntax problem, not the schema one."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        out = reg.dispatch("probe", {}, raw_args='{"text": "unclosed')
        assert isinstance(out, str)
        assert out.startswith("error: malformed JSON in arguments")
        assert "required property" not in out

    def test_honest_error_fires_without_repair_chain(self):
        """The honest-error check lives in `dispatch`, not in the
        repair chain — so it fires even on a registry with no
        handlers configured."""
        reg = ToolRegistry()  # no chain
        self._register(reg)
        out = reg.dispatch("probe", {}, raw_args='{"text": "unclosed')
        assert isinstance(out, str)
        assert out.startswith("error: malformed JSON in arguments")

    def test_genuinely_empty_payload_still_gets_schema_error(self):
        """Contrast: `{}` is *valid* JSON — the model really did send
        an empty object. That's a genuine missing-field error and
        must surface as such, NOT be masked as a JSON syntax error."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        out = reg.dispatch("probe", {}, raw_args='{}')
        assert isinstance(out, str)
        assert "required property" in out
        assert "malformed JSON" not in out

    def test_no_raw_args_still_gets_schema_error(self):
        """When the caller omits `raw_args` entirely there's nothing
        to re-parse — fall back to the schema error as before."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        out = reg.dispatch("probe", {})  # no raw_args
        assert isinstance(out, str)
        assert "required property" in out

    def test_recovered_call_does_not_trip_honest_error(self):
        """A payload the chain successfully recovers must dispatch
        normally — the honest-error check must not fire on a call
        that was repaired."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        self._register(reg)
        out = reg.dispatch("probe", {},
                            raw_args='{"text": "recovered", "extra": None}')
        assert isinstance(out, dict)
        assert out["got"]["text"] == "recovered"
