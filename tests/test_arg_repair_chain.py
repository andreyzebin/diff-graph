"""Argument-repair handler chain semantics.

The dispatch path now defers all arg-repair work to a chain of
`ArgRepairHandler` instances. These tests pin the **contract** of
that chain — independent of any specific handler implementation:

  - Repair is ONLY attempted when validation actually failed with a
    "missing required" error. Wrong-type / enum-violation / etc.
    pass through to the model unchanged.
  - Each handler is tried in order. The first one whose proposal
    re-validates wins; later handlers don't get a turn.
  - A handler returning None defers to the next handler with the
    ORIGINAL args (failed candidates don't poison the chain).
  - A handler raising an exception is logged and skipped, never
    crashes dispatch.
  - When the chain is empty (default, no fix-flag set), legacy
    behaviour: validation error returned verbatim.
  - Already-valid args never invoke any handler — the chain isn't
    even consulted on the happy path.

Concrete handler behaviour (TruncatedJsonHandler /
StringifiedArgsHandler) is pinned in test_qwen3_*.py — this file
is about the chain harness around them.
"""
from __future__ import annotations

import json
from typing import Optional

import pytest

from orchestra.tools.registry import ToolRegistry, ToolDef
from orchestra.tools.arg_repair import (
    ArgRepairHandler,
    TruncatedJsonHandler,
    StringifiedArgsHandler,
    default_qwen3_chain,
    _is_missing_required,
)


# ── A schema we can use across the handler-harness tests ────────────

SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "n": {"type": "integer"},
        "kind": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["text"],
}


def _register_probe(reg: ToolRegistry, *, name: str = "probe",
                    schema: Optional[dict] = None):
    reg.register_tool_def(ToolDef(
        name=name,
        description="probe",
        parameters=schema or SIMPLE_SCHEMA,
        handler=lambda **kw: {"got": kw},
    ))


# ── Trigger-gate: only "missing required" engages the chain ─────────

class TestMissingRequiredGate:
    """`_is_missing_required` is the trigger predicate every handler
    uses to decide whether to even try."""

    def test_matches_canonical_jsonschema_phrasing(self):
        assert _is_missing_required(
            "validation error: 'text' is a required property")

    def test_matches_path_prefixed_phrasing(self):
        """jsonschema sometimes emits `validation error at 'parent':
        'X' is a required property` — same suffix, prefixed path."""
        assert _is_missing_required(
            "validation error at 'a.b': 'text' is a required property")

    def test_does_not_match_type_violation(self):
        """Wrong type isn't a 'missing field' — repair handlers
        won't help. Let the model see the error."""
        assert not _is_missing_required(
            "validation error at 'n': 'foo' is not of type 'integer'")

    def test_does_not_match_enum_violation(self):
        assert not _is_missing_required(
            "validation error at 'kind': 'c' is not one of ['a', 'b']")

    def test_empty_or_none_is_false(self):
        """No error at all means no repair needed — predicate must
        be False so the chain short-circuits cleanly."""
        assert not _is_missing_required("")
        assert not _is_missing_required(None)


# ── Chain behaviour: ordering, skipping, failure isolation ──────────

class _RecorderHandler:
    """Test double — records every `try_repair` call. Configurable
    return value so we can probe both ordering and the
    next-handler-with-original-args invariant."""

    def __init__(self, name: str, candidate: Optional[dict] = None):
        self.name = name
        self.candidate = candidate
        self.calls: list[dict] = []

    def try_repair(self, *, raw_args, args, schema, error):
        self.calls.append({"raw_args": raw_args, "args": dict(args),
                            "error": error})
        return self.candidate


class _RaisingHandler:
    name = "boom"

    def try_repair(self, *, raw_args, args, schema, error):
        raise RuntimeError("synthetic handler crash")


class TestChainHarness:

    def test_valid_args_never_invoke_chain(self):
        """The happy path doesn't pay the handler-chain tax. A tool
        call with valid args dispatches straight to the handler
        without any try_repair call."""
        rec = _RecorderHandler("rec")
        reg = ToolRegistry(arg_repair_handlers=[rec])
        _register_probe(reg)
        result = reg.dispatch("probe", {"text": "hi"})
        assert result == {"got": {"text": "hi"}}
        assert rec.calls == [], "chain should not run on valid args"

    def test_non_missing_required_error_skips_chain(self):
        """Wrong type / enum / etc. validation failures don't trigger
        handlers — the model gets the original error so it can fix
        the actual problem. The harness-level gate enforces this:
        even a handler whose candidate WOULD pass validation never
        gets asked, because the trigger condition isn't met."""
        rec = _RecorderHandler("rec", candidate={"text": "hi", "n": 7})
        reg = ToolRegistry(arg_repair_handlers=[rec])
        _register_probe(reg)
        # `n` is required to be integer; we pass a string. Not a
        # missing-required error, so the chain must NOT engage —
        # confirmed via the recorder receiving zero calls.
        out = reg.dispatch("probe", {"text": "hi", "n": "not-an-int"})
        assert isinstance(out, str)  # original validation error returned
        assert "not of type" in out or "integer" in out
        assert rec.calls == [], (
            "harness-level gate should skip the chain entirely on "
            "non-missing-required errors"
        )

    def test_first_handler_wins_on_valid_proposal(self):
        """If handler A returns a candidate that re-validates,
        handler B never runs. Loop break is observable via the call
        log on B."""
        a = _RecorderHandler("A", candidate={"text": "from-A"})
        b = _RecorderHandler("B", candidate={"text": "from-B"})
        reg = ToolRegistry(arg_repair_handlers=[a, b])
        _register_probe(reg)
        out = reg.dispatch("probe", {})  # missing required: "text"
        assert out == {"got": {"text": "from-A"}}
        assert len(a.calls) == 1
        assert b.calls == [], "B must not run once A's repair was accepted"

    def test_handler_returning_none_yields_to_next(self):
        """When handler A defers (returns None), handler B is
        invoked with the same ORIGINAL args — A's non-attempt
        doesn't change the baseline."""
        a = _RecorderHandler("A", candidate=None)
        b = _RecorderHandler("B", candidate={"text": "B-rescued"})
        reg = ToolRegistry(arg_repair_handlers=[a, b])
        _register_probe(reg)
        out = reg.dispatch("probe", {})
        assert out == {"got": {"text": "B-rescued"}}
        assert len(a.calls) == 1 and len(b.calls) == 1
        # B saw the original args (empty), not A's discarded proposal
        assert b.calls[0]["args"] == {}

    def test_failed_proposal_does_not_poison_next_handler(self):
        """Handler A returns a candidate that doesn't re-validate.
        The chain rejects it; handler B is then invoked with the
        ORIGINAL args (not A's failed candidate)."""
        a = _RecorderHandler("A", candidate={"n": 1})  # still missing 'text'
        b = _RecorderHandler("B", candidate={"text": "ok"})
        reg = ToolRegistry(arg_repair_handlers=[a, b])
        _register_probe(reg)
        out = reg.dispatch("probe", {})
        assert out == {"got": {"text": "ok"}}
        # Baseline preserved: B saw {} not A's failed {"n": 1}
        assert b.calls[0]["args"] == {}

    def test_raising_handler_is_logged_and_skipped(self):
        """A handler that throws must not bring dispatch down. The
        chain continues; the next handler gets its turn; the
        original validation error surfaces only if NOTHING in the
        chain recovers."""
        b = _RecorderHandler("B", candidate={"text": "rescued"})
        reg = ToolRegistry(arg_repair_handlers=[_RaisingHandler(), b])
        _register_probe(reg)
        out = reg.dispatch("probe", {})
        assert out == {"got": {"text": "rescued"}}
        assert len(b.calls) == 1

    def test_all_handlers_defer_returns_original_error(self):
        """If every handler in the chain returns None, the
        validation error returned to the caller is the same one
        the chain was triggered on — no swallowing."""
        a = _RecorderHandler("A", candidate=None)
        b = _RecorderHandler("B", candidate=None)
        reg = ToolRegistry(arg_repair_handlers=[a, b])
        _register_probe(reg)
        out = reg.dispatch("probe", {})
        assert isinstance(out, str)
        assert "is a required property" in out or "required property" in out

    def test_empty_chain_returns_original_error(self):
        """Default-constructed registry has no handlers — dispatch
        behaves exactly like it did before the chain landed."""
        reg = ToolRegistry()  # no flag, no handlers
        _register_probe(reg)
        out = reg.dispatch("probe", {})
        assert isinstance(out, str)
        assert "text" in out

    def test_raw_args_threaded_to_handlers(self):
        """The raw arguments STRING is plumbed through `dispatch`
        kwarg so syntax-level handlers can re-attempt parse on the
        original. Each handler sees the exact string."""
        rec = _RecorderHandler("rec")
        reg = ToolRegistry(arg_repair_handlers=[rec])
        _register_probe(reg)
        reg.dispatch("probe", {}, raw_args='{"text": }')
        assert rec.calls[0]["raw_args"] == '{"text": }'


# ── End-to-end via the default qwen3 chain ──────────────────────────

class TestDefaultQwen3Chain:
    """The two-handler chain enabled by
    `ToolRegistry(fix_qwen3_stringification_bug=True)`. Exercises
    each handler through the full dispatch path."""

    def test_truncated_json_recovers_via_raw_args(self):
        """The exact qwen3 failure shape — broken JSON, empty args
        after fallback parse, raw string carrying the recoverable
        keys. End-to-end via the registry."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_probe(reg)
        raw = '{"text": "real text", "broken": }'
        out = reg.dispatch("probe", {}, raw_args=raw)
        assert out == {"got": {"text": "real text"}}

    def test_stringified_args_recovers_without_raw(self):
        """The other qwen3 shape — args parsed fine but everything's
        packed inside one string property. No raw_args needed.

        Schema requires BOTH `text` and `n` so the lift has a
        missing-required signal to act on (a `text`-only required
        schema would validate the outer wrapper as-is and never
        engage the chain)."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"},
                            "n": {"type": "integer"}},
            "required": ["text", "n"],
        }
        _register_probe(reg, schema=schema)
        bad = {"text": json.dumps({"text": "real text", "n": 7})}
        out = reg.dispatch("probe", bad)
        assert out["got"]["text"] == "real text"
        assert out["got"]["n"] == 7

    def test_default_chain_does_not_fire_on_wrong_type(self):
        """A non-missing-required validation error must NOT trigger
        the default chain — wrong type stays the model's problem."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        _register_probe(reg)
        out = reg.dispatch("probe", {"text": "ok", "n": "not-int"})
        assert isinstance(out, str)
        assert "not of type" in out or "integer" in out

    def test_default_chain_order(self):
        """The four built-ins must come back in the documented order:
        truncated_json (narrowest regex, args-empty), over_escaped_keys
        (global unescape, args-empty), python_literals (ast round-trip,
        args-empty), stringified_args (lift nested JSON, args-non-empty).
        Order matters for predictability when multiple handlers could
        theoretically apply."""
        chain = default_qwen3_chain()
        assert [h.name for h in chain] == [
            "truncated_json", "over_escaped_keys", "python_literals",
            "stringified_args",
        ]

    def test_clone_preserves_chain(self):
        """Cloning a registry copies the handler chain so a
        per-step force-reflect narrowing doesn't silently disable
        recovery half-way through a session."""
        reg = ToolRegistry(fix_qwen3_stringification_bug=True)
        cloned = reg.clone()
        assert [h.name for h in cloned.arg_repair_handlers] == \
               [h.name for h in reg.arg_repair_handlers]
