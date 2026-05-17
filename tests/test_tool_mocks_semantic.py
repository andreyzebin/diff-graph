"""Semantic mock-matching tests — the `{mode: semantic}` ToolMocks
shape that routes by intent (LLM judge or other strategy) instead
of strict ordinal.

Architecture pinned here:
- YAML shape parses into SemanticConfig + SemanticFixture (with
  validation surfacing typos at load time, not run time).
- LLM caller is INJECTED via ToolMocks.set_llm_caller; the strategy
  layer is provider-agnostic.
- llm_judge_match parses both bare JSON and code-fenced JSON.
- consume() returns the matched fixture's return_data wrapped as a
  MockEntry so the rest of the dispatch path is unchanged.
- on_no_match: stub returns the neutral NO_MATCH_STUB; error raises
  MockExhaustedError.
"""
from __future__ import annotations

import json

import pytest

from orchestra.tool_mocks import MockExhaustedError, ToolMocks
from orchestra.tool_mocks_semantic import (
    NO_MATCH_STUB,
    MatchResult,
    SemanticConfig,
    SemanticFixture,
    llm_judge_match,
)


# ── YAML / from_dict parsing ───────────────────────────────────────


class TestSemanticParse:

    def _yaml_block(self):
        return {
            "agent_spawn": {
                "mode": "semantic",
                "match": {
                    "strategy": "llm_judge",
                    "model": "deepseek-chat",
                    "threshold": 0.7,
                    "on_no_match": "stub",
                },
                "fixtures": [
                    {
                        "examples": ["check ownership in service",
                                     "verify customer owns the credit"],
                        "return": "INVESTIGATION (ownership): missing check.",
                    },
                    {
                        "examples": ["partial consumption flow"],
                        "return": "INVESTIGATION (partial): broken.",
                    },
                ],
            }
        }

    def test_parses_into_semantic_by_tool(self):
        m = ToolMocks.from_dict(self._yaml_block())
        assert "agent_spawn" in m.semantic_by_tool
        # Not duplicated into the ordinal map.
        assert "agent_spawn" not in m.by_tool
        cfg = m.semantic_by_tool["agent_spawn"]
        assert cfg.strategy == "llm_judge"
        assert cfg.model == "deepseek-chat"
        assert cfg.threshold == 0.7
        assert cfg.on_no_match == "stub"
        assert cfg.on_arg == "focus"          # default
        assert len(cfg.fixtures) == 2
        assert cfg.fixtures[0].examples[0] == "check ownership in service"

    def test_has_reports_semantic_tool(self):
        m = ToolMocks.from_dict(self._yaml_block())
        assert m.has("agent_spawn") is True

    def test_defaults_when_match_block_omits_fields(self):
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "fixtures": [{"examples": ["x"], "return": "y"}],
            },
        })
        cfg = m.semantic_by_tool["agent_spawn"]
        assert cfg.strategy == "llm_judge"
        assert cfg.threshold == 0.6
        assert cfg.on_no_match == "stub"
        assert cfg.model is None

    def test_invalid_on_no_match_raises(self):
        with pytest.raises(ValueError, match="on_no_match"):
            ToolMocks.from_dict({
                "agent_spawn": {
                    "mode": "semantic",
                    "match": {"on_no_match": "bogus"},
                    "fixtures": [{"examples": ["x"], "return": "y"}],
                },
            })

    def test_empty_fixtures_raises(self):
        with pytest.raises(ValueError, match="fixtures"):
            ToolMocks.from_dict({
                "agent_spawn": {"mode": "semantic", "fixtures": []},
            })

    def test_fixture_missing_examples_raises(self):
        with pytest.raises(ValueError, match="examples"):
            ToolMocks.from_dict({
                "agent_spawn": {
                    "mode": "semantic",
                    "fixtures": [{"return": "no examples"}],
                },
            })

    def test_fixture_missing_return_raises(self):
        with pytest.raises(ValueError, match="return"):
            ToolMocks.from_dict({
                "agent_spawn": {
                    "mode": "semantic",
                    "fixtures": [{"examples": ["x"]}],
                },
            })


# ── llm_judge strategy ─────────────────────────────────────────────


class TestLLMJudgeStrategy:

    def _fixtures(self):
        return [
            SemanticFixture(
                examples=["check ownership", "verify customer owns credit"],
                return_data="ownership response",
            ),
            SemanticFixture(
                examples=["partial consumption when credit > order"],
                return_data="partial response",
            ),
        ]

    def _judge_caller(self, response: str):
        captured: dict = {}
        def _call(messages, *, model=None):
            captured["messages"] = messages
            captured["model"] = model
            return response
        return _call, captured

    def test_parses_clean_json(self):
        caller, captured = self._judge_caller(
            '{"match": 1, "confidence": 0.9, "reason": "ownership"}'
        )
        r = llm_judge_match(
            "is the customer-credit linkage validated",
            self._fixtures(), model="deepseek-chat", llm_caller=caller,
        )
        assert r.index == 0          # 1-based 1 → 0-based 0
        assert r.confidence == 0.9
        assert "ownership" in r.reason

    def test_parses_code_fenced_json(self):
        caller, _ = self._judge_caller(
            '```json\n{"match": 2, "confidence": 0.85, "reason": "partial"}\n```'
        )
        r = llm_judge_match(
            "x", self._fixtures(), model="x", llm_caller=caller,
        )
        assert r.index == 1
        assert r.confidence == 0.85

    def test_zero_match_signals_no_match(self):
        caller, _ = self._judge_caller(
            '{"match": 0, "confidence": 0.5, "reason": "unrelated"}'
        )
        r = llm_judge_match(
            "x", self._fixtures(), model="x", llm_caller=caller,
        )
        assert r.index == -1
        assert "unrelated" in r.reason

    def test_out_of_range_match_treated_as_no_match(self):
        caller, _ = self._judge_caller(
            '{"match": 99, "confidence": 0.9, "reason": "?"}'
        )
        r = llm_judge_match(
            "x", self._fixtures(), model="x", llm_caller=caller,
        )
        assert r.index == -1

    def test_unparseable_returns_no_match(self):
        caller, _ = self._judge_caller("I have no idea what to say")
        r = llm_judge_match(
            "x", self._fixtures(), model="x", llm_caller=caller,
        )
        assert r.index == -1
        assert "unparseable" in r.reason.lower()

    def test_prompt_includes_all_fixtures_and_actual(self):
        caller, captured = self._judge_caller(
            '{"match": 1, "confidence": 0.9}'
        )
        llm_judge_match(
            "the actual focus", self._fixtures(),
            model="m1", llm_caller=caller,
        )
        prompt_text = captured["messages"][0]["content"]
        assert "the actual focus" in prompt_text
        assert "check ownership" in prompt_text
        assert "partial consumption" in prompt_text
        # 1..N enumeration; both candidates appear.
        assert "[1]" in prompt_text
        assert "[2]" in prompt_text
        # Model passthrough.
        assert captured["model"] == "m1"


# ── ToolMocks.consume() through semantic path ──────────────────────


class TestSemanticConsume:

    def _build(self, judge_response: str, threshold: float = 0.6,
               on_no_match: str = "stub"):
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "match": {
                    "strategy": "llm_judge",
                    "threshold": threshold,
                    "on_no_match": on_no_match,
                },
                "fixtures": [
                    {"examples": ["ownership check"],
                     "return": "ownership-OK"},
                    {"examples": ["partial consumption"],
                     "return": "partial-OK"},
                ],
            },
        })
        m.set_llm_caller(lambda messages, *, model=None: judge_response)
        return m

    def test_match_above_threshold_returns_fixture(self):
        m = self._build('{"match": 1, "confidence": 0.9}', threshold=0.6)
        entry = m.consume("agent_spawn",
                          {"agent": "investigator", "focus": "verify ownership"})
        assert entry.return_data == "ownership-OK"

    def test_match_below_threshold_falls_back_to_stub(self):
        m = self._build('{"match": 1, "confidence": 0.4}', threshold=0.7)
        entry = m.consume("agent_spawn", {"focus": "x"})
        assert entry.return_data == NO_MATCH_STUB
        # Stub is sticky — subsequent calls still get a stub, not exhausted.
        entry2 = m.consume("agent_spawn", {"focus": "y"})
        assert entry2.return_data == NO_MATCH_STUB

    def test_no_match_with_on_no_match_error_raises(self):
        m = self._build('{"match": 0, "confidence": 0.2}',
                        on_no_match="error")
        with pytest.raises(MockExhaustedError, match="semantic match failed"):
            m.consume("agent_spawn", {"focus": "unrelated thing"})

    def test_uses_on_arg_field(self):
        # Default is `focus`; verify by overriding with a different
        # arg field and checking the LLM caller saw it.
        captured = {}
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "match": {"on_arg": "question"},
                "fixtures": [
                    {"examples": ["a"], "return": "r"},
                ],
            },
        })
        def _caller(messages, *, model=None):
            captured["prompt"] = messages[0]["content"]
            return '{"match": 1, "confidence": 0.99}'
        m.set_llm_caller(_caller)
        m.consume("agent_spawn", {"question": "OWNERSHIP CHECK"})
        assert "OWNERSHIP CHECK" in captured["prompt"]

    def test_no_caller_set_raises_with_clear_message(self):
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "fixtures": [{"examples": ["a"], "return": "r"}],
            },
        })
        # Note: no set_llm_caller() call.
        with pytest.raises(RuntimeError, match="set_llm_caller"):
            m.consume("agent_spawn", {"focus": "anything"})


# ── Coexistence with ordinal + capture_only modes ──────────────────


class TestModeCoexistence:
    """Same mocks file can mix mode types — semantic for agent_spawn,
    ordinal for pr_post_comment, capture_only for set_review_status."""

    def test_three_modes_in_one_fixture(self):
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "fixtures": [{"examples": ["a"], "return": "semantic-r"}],
            },
            "pr_post_comment": [{"return": "ordinal-r"}],
            "set_review_status": {"mode": "capture_only"},
        })
        assert m.has("agent_spawn")
        assert m.has("pr_post_comment")
        assert m.has("set_review_status")
        # Routing: semantic_by_tool vs by_tool placement.
        assert "agent_spawn" in m.semantic_by_tool
        assert "pr_post_comment" in m.by_tool
        assert "set_review_status" in m.by_tool

    def test_ordinal_unaffected_by_semantic_neighbour(self):
        m = ToolMocks.from_dict({
            "agent_spawn": {
                "mode": "semantic",
                "fixtures": [{"examples": ["a"], "return": "semantic-r"}],
            },
            "pr_post_comment": [{"return": "first"}, {"return": "second"}],
        })
        # Ordinal consume still works the old way.
        assert m.consume("pr_post_comment", {}).return_data == "first"
        assert m.consume("pr_post_comment", {}).return_data == "second"


# ── render_mock_result: bare-string envelope ───────────────────────


class TestRenderEnvelope:
    """The semantic-fixture return is a bare string (the investigator
    summary text). render_mock_result must propagate it into the
    spawn envelope's `output` field — anything else hides the
    response from the agent. Regression test for the
    "empty {} output" bug on REV-U-008 plan 284 where the legacy
    envelope-builder treated string returns as `ret={}` and emitted
    `"output": "{}"`."""

    def test_string_return_lands_in_output(self):
        from orchestra.tool_mocks import MockEntry, render_mock_result
        entry = MockEntry(
            when={}, return_data="INVESTIGATION (ownership): missing check.",
            sticky=True,
        )
        raw = render_mock_result("agent_spawn", entry,
                                  {"agent": "investigator", "focus": "x"})
        envelope = json.loads(raw)
        assert envelope["output"] == "INVESTIGATION (ownership): missing check."
        assert envelope["status"] == "completed"
        assert envelope["agent_name"] == "investigator"
        assert envelope["mocked"] is True

    def test_capture_only_stub_string_propagates(self):
        """The capture_only stub is a JSON string by design — it
        should ALSO land in `output` verbatim instead of being
        re-wrapped into `{}`. Same fast-path as semantic."""
        from orchestra.tool_mocks import MockEntry, render_mock_result
        stub = '{"status":"spawned","child_id":"<test-stub>","mode":"capture_only"}'
        raw = render_mock_result(
            "agent_spawn", MockEntry(when={}, return_data=stub, sticky=True),
            {"agent": "investigator"},
        )
        envelope = json.loads(raw)
        assert envelope["output"] == stub

    def test_dict_return_legacy_path_preserved(self):
        """Existing structured fixtures with `{findings: [...]}` keep
        working — bare-string path is additive, not replacing."""
        from orchestra.tool_mocks import MockEntry, render_mock_result
        entry = MockEntry(when={}, return_data={
            "findings": [{"severity": "BLOCKER", "title": "x"}],
            "confidence": "high",
            "learned": "found a thing",
        }, sticky=True)
        raw = render_mock_result("agent_spawn", entry,
                                  {"agent": "investigator"})
        envelope = json.loads(raw)
        parsed_output = json.loads(envelope["output"])
        assert parsed_output["findings"][0]["severity"] == "BLOCKER"
        assert "confidence=high" in envelope["sgr_summary"]
