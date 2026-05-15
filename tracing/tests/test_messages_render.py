"""Server-side rendering of an OpenAI-style messages array into a
human-readable transcript.

Why the renderer lives on the server (not in the UI / CLI):
  - The QA UI's `history` tab and the `quality-cli traces messages`
    command both want the same format. Putting the renderer in one
    place keeps them in sync.
  - The "as=text" view of /messages and /call is part of the public
    API contract — tooling can rely on it as the canonical
    human-readable view without re-implementing the stitching.

These tests pin the renderer's contract directly. Endpoint-level
plumbing tests live in `test_messages_render_api.py`.
"""
from __future__ import annotations

import pytest

from tracing.server.messages_render import (
    render_messages,
    render_call,
)


class TestRenderMessagesShape:

    def test_system_user_assistant_text_get_role_banners(self):
        """The three plain-text role types render with their banner
        (uppercased role + box-drawing bar) followed by the content
        verbatim, in input order."""
        out = render_messages([
            {"role": "system", "content": "You are a reviewer."},
            {"role": "user", "content": "Review this PR."},
            {"role": "assistant", "content": "OK, reading the diff."},
        ])
        assert "SYSTEM" in out
        assert "USER" in out
        assert "ASSISTANT" in out
        assert "You are a reviewer." in out
        assert "Review this PR." in out
        assert "OK, reading the diff." in out
        # Ordering: SYSTEM block precedes USER which precedes ASSISTANT.
        assert out.index("SYSTEM") < out.index("USER") < out.index("ASSISTANT")

    def test_tool_call_args_pretty_printed_when_json(self):
        """Tool-call arguments arrive as a JSON-encoded STRING (OpenAI
        wire format). The renderer parses + pretty-prints so multi-key
        payloads aren't a single illegible line in the transcript."""
        out = render_messages([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "diff_read_file",
                        "arguments": '{"path":"src/Foo.java","start_line":40}',
                    },
                }],
            },
        ])
        assert "ASSISTANT → TOOL CALL" in out
        assert "▶ diff_read_file" in out
        # Indented pretty form, not the original one-liner.
        assert '"path": "src/Foo.java"' in out
        assert '"start_line": 40' in out

    def test_malformed_tool_call_args_kept_verbatim(self):
        """Some providers (qwen3-coder via vLLM, see qwen-code#783)
        leak `<parameter>` XML fragments into the arguments string. We
        keep them as-is so a debugger can see the raw shape rather
        than masking the failure mode."""
        bad = "<parameter=path>src/Foo.java</parameter>"
        out = render_messages([{
            "role": "assistant",
            "tool_calls": [{
                "function": {"name": "diff_read_file", "arguments": bad},
            }],
        }])
        assert "▶ diff_read_file" in out
        assert "<parameter=path>" in out

    def test_tool_result_carries_tool_name_and_call_id(self):
        """`tool` role messages render with the tool's name + the
        call-id linking back to the matching call earlier in the
        transcript. Lets a reader trace a result back to its trigger."""
        out = render_messages([{
            "role": "tool",
            "name": "diff_read_file",
            "tool_call_id": "call_1",
            "content": "lines 40-60: ...",
        }])
        assert "TOOL RESULT · diff_read_file" in out
        assert "call=call_1" in out
        assert "lines 40-60: ..." in out

    def test_assistant_with_both_content_and_tool_calls(self):
        """Some models (deepseek, qwen3) emit reasoning text alongside
        the tool call. Render content first, then the call(s), so the
        reader sees the model's stated intent before the payload."""
        out = render_messages([{
            "role": "assistant",
            "content": "I'll check Foo.java first.",
            "tool_calls": [{"function": {"name": "diff_read_file",
                                          "arguments": '{"path":"Foo.java"}'}}],
        }])
        # Both parts present, content before call.
        assert "I'll check Foo.java first." in out
        assert "▶ diff_read_file" in out
        assert out.index("I'll check Foo.java first.") < out.index("▶ diff_read_file")

    def test_empty_content_is_explicit(self):
        """An empty assistant.content is rendered as `(empty)` rather
        than blanking — the reader can tell the model intentionally
        produced nothing vs the renderer dropping the field."""
        out = render_messages([{"role": "assistant", "content": ""}])
        assert "(empty)" in out

    def test_non_dict_entries_are_skipped(self):
        """Defensive: don't crash on a polluted array (e.g. legacy
        trace blobs sometimes have None slots)."""
        out = render_messages([
            {"role": "user", "content": "hi"},
            None,
            "garbage",
            {"role": "assistant", "content": "ok"},
        ])
        assert "hi" in out
        assert "ok" in out


class TestRenderCall:
    """Single-turn outbound payload — the /call endpoint shape."""

    def test_tool_call_payload(self):
        out = render_call({
            "content": "",
            "tool_calls": [{
                "function": {"name": "diff_outline",
                              "arguments": '{"path":"src/Foo.java"}'},
            }],
        })
        assert "▶ diff_outline" in out
        assert '"path": "src/Foo.java"' in out

    def test_text_only_payload(self):
        """Judge / mode:single agents return content only — render that
        as-is, no `▶` marker."""
        out = render_call({
            "content": "The review is approved.",
            "tool_calls": [],
        })
        assert out.strip() == "The review is approved."

    def test_content_plus_tool_calls(self):
        """Same convention as the transcript: content first, calls
        after."""
        out = render_call({
            "content": "Found the bug.",
            "tool_calls": [{"function": {"name": "pr_post_comment",
                                          "arguments": '{"text":"BLOCKER"}'}}],
        })
        assert "Found the bug." in out
        assert "▶ pr_post_comment" in out
        assert out.index("Found the bug.") < out.index("▶ pr_post_comment")

    def test_truly_empty_payload(self):
        """No content, no tool_calls — explicit `(empty)` marker so
        the reader doesn't think the fetch failed silently."""
        out = render_call({"content": "", "tool_calls": []})
        assert out.strip() == "(empty)"

    def test_non_dict_safe(self):
        assert render_call(None) == ""
        assert render_call([]) == ""
