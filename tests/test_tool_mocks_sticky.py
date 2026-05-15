"""Sticky / one-line-shortcut semantics of `orchestra.tool_mocks.ToolMocks`.

The legacy contract is strict-ordinal: i-th call consumes the i-th
entry. These tests pin the two extensions added on top:

1. `sticky: true` on an entry makes that entry replay forever — the
   counter stops advancing the moment a sticky entry is consumed.
2. A bare string as the tool's YAML value is a shortcut for a single
   sticky entry returning that string. Designed for the common case
   "I want this tool disabled with a canned reply" — one line of
   config instead of three.

Sister file: `orchestra/tool_mocks.py`. The legacy strict-ordinal
behaviour for non-sticky lists is also re-pinned here so future
refactors can't accidentally break it.
"""
from __future__ import annotations

import textwrap

import pytest
import yaml

from orchestra.tool_mocks import ToolMocks, MockExhaustedError


# ── Strict-ordinal regression (legacy contract still holds) ─────────

class TestStrictOrdinalLegacy:

    def test_each_entry_consumed_once_in_order(self):
        mocks = ToolMocks.from_dict({
            "pr_post_comment": [
                {"return": "ok-1"},
                {"return": "ok-2"},
                {"return": "ok-3"},
            ],
        })
        assert mocks.consume("pr_post_comment", {}).return_data == "ok-1"
        assert mocks.consume("pr_post_comment", {}).return_data == "ok-2"
        assert mocks.consume("pr_post_comment", {}).return_data == "ok-3"

    def test_exhaustion_raises(self):
        mocks = ToolMocks.from_dict({
            "pr_post_comment": [{"return": "only"}],
        })
        mocks.consume("pr_post_comment", {})
        with pytest.raises(MockExhaustedError):
            mocks.consume("pr_post_comment", {})

    def test_unconfigured_tool_raises(self):
        mocks = ToolMocks.from_dict({"pr_post_comment": [{"return": "x"}]})
        with pytest.raises(MockExhaustedError):
            mocks.consume("set_review_status", {})


# ── Sticky entries replay forever ────────────────────────────────────

class TestStickyEntries:

    def test_single_sticky_entry_replays_indefinitely(self):
        """The common shape: one sticky entry → tool returns the same
        canned value on every call, forever, no counter advance."""
        mocks = ToolMocks.from_dict({
            "set_review_status": [
                {"sticky": True, "return": "disabled — proceed"},
            ],
        })
        for _ in range(50):
            entry = mocks.consume("set_review_status", {})
            assert entry.return_data == "disabled — proceed"
            assert entry.sticky is True

    def test_sticky_at_tail_after_ordinal_prefix(self):
        """Mixed: two ordinal entries followed by a sticky fallback.
        Calls 1-2 consume entries 0-1, calls 3+ all stick on entry 2."""
        mocks = ToolMocks.from_dict({
            "pr_post_comment": [
                {"return": "first-real"},
                {"return": "second-real"},
                {"sticky": True, "return": "all subsequent calls"},
            ],
        })
        assert mocks.consume("pr_post_comment", {}).return_data == "first-real"
        assert mocks.consume("pr_post_comment", {}).return_data == "second-real"
        for _ in range(10):
            entry = mocks.consume("pr_post_comment", {})
            assert entry.return_data == "all subsequent calls"

    def test_sticky_at_head_blocks_later_entries(self):
        """A sticky entry at position 0 means all subsequent positions
        are dead code — the counter never advances past 0. Useful to
        understand: sticky position matters, place it last for fallback
        semantics, place it first to short-circuit everything."""
        mocks = ToolMocks.from_dict({
            "pr_post_comment": [
                {"sticky": True, "return": "always-this"},
                {"return": "never-reached-1"},
                {"return": "never-reached-2"},
            ],
        })
        for _ in range(5):
            assert mocks.consume(
                "pr_post_comment", {}
            ).return_data == "always-this"

    def test_sticky_false_explicit_behaves_as_legacy(self):
        """`sticky: false` is the default — pinning that explicit
        false doesn't accidentally make the entry sticky."""
        mocks = ToolMocks.from_dict({
            "pr_post_comment": [
                {"sticky": False, "return": "one-shot"},
            ],
        })
        assert mocks.consume("pr_post_comment", {}).return_data == "one-shot"
        with pytest.raises(MockExhaustedError):
            mocks.consume("pr_post_comment", {})


# ── One-line shortcut: bare string → sticky single-entry ─────────────

class TestStringShortcut:

    def test_bare_string_becomes_sticky_single_entry(self):
        """`tool_name: "msg"` is equivalent to `tool_name: [{sticky:
        true, return: "msg"}]`. The most common case for the user-
        facing scenario `set_review_status temporarily disabled`."""
        mocks = ToolMocks.from_dict({
            "set_review_status": "tool off — continue",
        })
        for _ in range(20):
            entry = mocks.consume("set_review_status", {})
            assert entry.return_data == "tool off — continue"
            assert entry.sticky is True

    def test_shortcut_via_yaml_round_trip(self, tmp_path):
        """End-to-end through `from_yaml` — the shortcut must survive
        YAML parsing (yaml.safe_load returns a string, not a list)."""
        fixture = tmp_path / "disable.yaml"
        fixture.write_text(textwrap.dedent("""\
            set_review_status: "review status temporarily disabled"
            pr_post_comment:
              - return: "real-first"
              - sticky: true
                return: "real-rest"
        """), encoding="utf-8")
        mocks = ToolMocks.from_yaml(fixture)

        # Shortcut form survived.
        assert mocks.consume(
            "set_review_status", {}
        ).return_data == "review status temporarily disabled"
        assert mocks.consume(
            "set_review_status", {}
        ).return_data == "review status temporarily disabled"

        # Explicit form alongside it still works.
        assert mocks.consume(
            "pr_post_comment", {}
        ).return_data == "real-first"
        for _ in range(3):
            assert mocks.consume(
                "pr_post_comment", {}
            ).return_data == "real-rest"

    def test_shortcut_does_not_affect_other_tools(self):
        """A string shortcut on one tool doesn't poison the unrelated
        tools' ordinal counters."""
        mocks = ToolMocks.from_dict({
            "set_review_status": "off",
            "pr_post_comment": [{"return": "x"}, {"return": "y"}],
        })
        # Burn through pr_post_comment normally.
        assert mocks.consume("pr_post_comment", {}).return_data == "x"
        assert mocks.consume("pr_post_comment", {}).return_data == "y"
        with pytest.raises(MockExhaustedError):
            mocks.consume("pr_post_comment", {})
        # set_review_status stays sticky regardless.
        assert mocks.consume(
            "set_review_status", {}
        ).return_data == "off"


# ── Load-time validation ────────────────────────────────────────────

class TestLoaderValidation:

    def test_invalid_value_type_raises(self):
        """Neither list nor string — e.g. someone wrote a dict at the
        top level by mistake. Loader rejects with a clear message."""
        with pytest.raises(ValueError, match="must be a list or string"):
            ToolMocks.from_dict({"pr_post_comment": 42})

    def test_entry_missing_return_field(self):
        """List form still requires `return:` on every entry — sticky
        flag alone isn't enough to define what to return."""
        with pytest.raises(ValueError, match="missing 'return'"):
            ToolMocks.from_dict({
                "pr_post_comment": [{"sticky": True}],
            })

    def test_yaml_parses_to_string_value(self, tmp_path):
        """Sanity check: yaml.safe_load actually produces a `str` when
        the YAML right-hand side is a quoted string. If PyYAML ever
        changes that, the shortcut path would silently break."""
        fixture = tmp_path / "shortcut.yaml"
        fixture.write_text('set_review_status: "x"\n', encoding="utf-8")
        data = yaml.safe_load(fixture.read_text(encoding="utf-8"))
        assert isinstance(data["set_review_status"], str)
