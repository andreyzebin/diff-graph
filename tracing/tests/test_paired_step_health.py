"""Per-step health flags surfaced in the prepared trace tree.

The QA session-trace UI shows each LLM step as one row in the tree.
Without health flags a reader has to expand every step to notice:

  - tool calls that came back as `validation error` (the agent
    *tried* to do something but every result was a parse/schema
    failure — see the qwen3 `"parent_id": }` case where all 6
    post_comment calls failed),
  - the same outbound payload re-emitted after seeing an error (a
    loop indicator — the model isn't learning from the feedback).

`_prepare_agent` now annotates each paired step with:

  - `tool_errors_count` — integer, how many of this step's tool
    results matched a known framework-error prefix.
  - `repeats_prev_step` — bool, True when this step's tool-call
    signature is identical to the previous step's.

These tests pin the contract. UI badge styling is a downstream
concern; the data has to be correct first.
"""
from __future__ import annotations

import pytest

from orchestra.trace import (
    _prepare_agent,
    _looks_like_tool_error,
    _tool_call_signature,
)


# ── Helper-level pins ──────────────────────────────────────────────

class TestLooksLikeToolError:

    def test_validation_error_prefix(self):
        """The framework's schema validator emits
        `validation error at 'x': …` — base case to detect."""
        assert _looks_like_tool_error("validation error: 'text' is a required property")

    def test_unknown_tool_prefix(self):
        """Returned by registry.dispatch when the model invents a tool
        name that doesn't exist."""
        assert _looks_like_tool_error("unknown tool: post_commen")

    def test_error_colon_prefix(self):
        """Generic tool handler error shape used by several domain
        tools (`return f"error: …"`)."""
        assert _looks_like_tool_error("error: file not found")

    def test_case_insensitive(self):
        """Don't break if a tool capitalises the prefix differently."""
        assert _looks_like_tool_error("Validation Error: x missing")

    def test_normal_tool_output_not_error(self):
        """A well-formed result shouldn't be flagged as an error just
        because it happens to contain the word 'error' elsewhere."""
        assert not _looks_like_tool_error("posted comment id=42")
        assert not _looks_like_tool_error(
            "found 3 matches; one of them mentions 'error'")

    def test_non_string_safe(self):
        """tool_results can be malformed in old traces — defensive."""
        assert not _looks_like_tool_error(None)
        assert not _looks_like_tool_error(42)


class TestToolCallSignature:

    def test_extracts_name_and_args(self):
        """Two steps with the SAME tool_calls produce the same sig.
        Loop detector compares these tuples for equality."""
        resp = {"tool_calls": [
            {"name": "post_comment", "arguments": '{"text":"hi"}'},
        ]}
        assert _tool_call_signature(resp) == (("post_comment", '{"text":"hi"}'),)

    def test_handles_openai_function_envelope(self):
        """OpenAI wire shape nests under `function.{name, arguments}`.
        The signature helper unwraps either flat or nested."""
        resp = {"tool_calls": [{
            "function": {"name": "diff_outline", "arguments": '{"path":"X.java"}'},
        }]}
        assert _tool_call_signature(resp) == (("diff_outline", '{"path":"X.java"}'),)

    def test_empty_tool_calls_returns_none(self):
        """Text-only steps (judge / final done) have no tool_calls.
        Returning None — not an empty tuple — keeps two text-only
        steps from accidentally comparing equal as repeats."""
        assert _tool_call_signature({"tool_calls": []}) is None
        assert _tool_call_signature({}) is None
        assert _tool_call_signature(None) is None

    def test_signature_order_matters(self):
        """Different ordering of parallel tool calls = different sig.
        Conservative: don't claim repeat unless the calls match in
        order too. (Real loops re-emit the same order.)"""
        a = {"tool_calls": [{"name": "x", "arguments": ""}, {"name": "y", "arguments": ""}]}
        b = {"tool_calls": [{"name": "y", "arguments": ""}, {"name": "x", "arguments": ""}]}
        assert _tool_call_signature(a) != _tool_call_signature(b)


# ── _prepare_agent integration pins ───────────────────────────────

def _minimal_trace(steps):
    """Build the dict shape `_prepare_agent` consumes. Each step in
    `steps` is a (request_messages, response_dict) pair. The flat
    `llm_calls` array carries one `type:request` and one
    `type:response` entry per step — exactly what TraceCollector
    produces at runtime."""
    llm_calls = []
    for i, (req_msgs, resp) in enumerate(steps):
        llm_calls.append({"step": i, "type": "request", "messages": req_msgs})
        # response entry needs `tool_calls` field at the top level
        # (not nested under `resp:`); _prepare_agent reads it directly.
        resp_entry = {"step": i, "type": "response"}
        resp_entry.update(resp or {})
        llm_calls.append(resp_entry)
    return {
        "agent_id": "ag1",
        "agent_name": "probe",
        "steps": len(steps),
        "tokens_paid": 0,
        "sgr": [],
        "llm_calls": llm_calls,
        "children": [],
        "output": None,
    }


class TestToolErrorsCount:

    def test_single_step_no_results_zero(self):
        """The final step in a trace has no `next` step to draw tool
        results from — the count must default to 0, not None."""
        prepared = _prepare_agent(_minimal_trace([
            ([], {"tool_calls": [{"name": "done", "arguments": "{}"}]}),
        ]), depth=0)
        assert prepared["paired_steps"][0]["tool_errors_count"] == 0

    def test_counts_validation_errors_in_next_step_messages(self):
        """Reproduces the actual failure shape: step 0 emits 3 tool
        calls, all results come back as `validation error: ...` in
        step 1's request messages."""
        steps = [
            # step 0: emit 3 post_comment calls
            ([], {"tool_calls": [
                {"name": "post_comment", "arguments": '{"text":"a"}'},
                {"name": "post_comment", "arguments": '{"text":"b"}'},
                {"name": "post_comment", "arguments": '{"text":"c"}'},
            ]}),
            # step 1: req contains the 3 tool results — all errors
            ([
                {"role": "tool", "content": "validation error: 'text' is a required property"},
                {"role": "tool", "content": "validation error: 'text' is a required property"},
                {"role": "tool", "content": "validation error: 'text' is a required property"},
            ], {"tool_calls": []}),
        ]
        prepared = _prepare_agent(_minimal_trace(steps), depth=0)
        # Step 0 had 3 outbound calls, all 3 came back as errors.
        assert prepared["paired_steps"][0]["tool_errors_count"] == 3
        # Step 1 made no outbound calls and has nothing after it.
        assert prepared["paired_steps"][1]["tool_errors_count"] == 0

    def test_mixed_results_only_errors_counted(self):
        """A step that mixes one error + one successful result counts
        only the error. Lets the UI badge "1/3 failed" semantics
        without us having to also surface a success-count column."""
        steps = [
            ([], {"tool_calls": [
                {"name": "post_comment", "arguments": '{"text":"a"}'},
                {"name": "post_comment", "arguments": '{"text":"b"}'},
                {"name": "post_comment", "arguments": '{"text":"c"}'},
            ]}),
            ([
                {"role": "tool", "content": "validation error: 'text' is required"},
                {"role": "tool", "content": "posted comment id=42"},
                {"role": "tool", "content": "unknown tool: post_commen"},
            ], {}),
        ]
        prepared = _prepare_agent(_minimal_trace(steps), depth=0)
        assert prepared["paired_steps"][0]["tool_errors_count"] == 2


class TestRepeatsPrevStep:

    def test_identical_tool_calls_flagged_as_repeat(self):
        """Two consecutive steps with the SAME tool_calls signature —
        the loop indicator the UI badges. Models stuck in a JSON-
        validation loop do exactly this: same payload, same error,
        same next attempt."""
        same_call = {"name": "post_comment", "arguments": '{"text":"hi"}'}
        steps = [
            ([], {"tool_calls": [same_call]}),
            ([{"role": "tool", "content": "validation error"}],
             {"tool_calls": [same_call]}),
        ]
        prepared = _prepare_agent(_minimal_trace(steps), depth=0)
        # The FIRST step has no prior step to compare against.
        assert prepared["paired_steps"][0]["repeats_prev_step"] is False
        # The SECOND step is identical to the first → repeat.
        assert prepared["paired_steps"][1]["repeats_prev_step"] is True

    def test_different_args_not_a_repeat(self):
        """Different arguments break the signature equality even when
        the tool name matches — the model is making progress, just
        slowly. Not flagged."""
        steps = [
            ([], {"tool_calls": [{"name": "diff_read_file",
                                    "arguments": '{"path":"A.java"}'}]}),
            ([], {"tool_calls": [{"name": "diff_read_file",
                                    "arguments": '{"path":"B.java"}'}]}),
        ]
        prepared = _prepare_agent(_minimal_trace(steps), depth=0)
        assert prepared["paired_steps"][1]["repeats_prev_step"] is False

    def test_text_only_step_never_repeats(self):
        """A text-only step (judge, mode:single, final done()) has no
        tool_calls signature; even two adjacent text-only steps must
        NOT both flag as repeats of each other — the indicator means
        'tool loop', not 'two text answers in a row'."""
        steps = [
            ([], {"content": "thinking…", "tool_calls": []}),
            ([], {"content": "still thinking…", "tool_calls": []}),
        ]
        prepared = _prepare_agent(_minimal_trace(steps), depth=0)
        assert prepared["paired_steps"][0]["repeats_prev_step"] is False
        assert prepared["paired_steps"][1]["repeats_prev_step"] is False
