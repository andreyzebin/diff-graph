"""Tests for orchestra.tools.meta — list_tools / call_tool framework.

Pins the contract for the meta dispatch mode: bounded view of the
registry, structured-error responses on misuse, lenient args parsing
(JSON-string passthrough), and the LLM-visible schema stability.
"""
from __future__ import annotations

import json

import pytest

from orchestra.tools.registry import ToolRegistry
from orchestra.tools.meta import build_meta_tools


@pytest.fixture
def reg_with_tools():
    """Three diverse tools: read_file (real handler with required),
    submit_answer (capture-style), echo (no required)."""
    reg = ToolRegistry()

    def _read_file(path: str, start_line: int = 1) -> dict:
        return {"path": path, "from": start_line, "content": "<elided>"}

    reg.register(
        fn=_read_file,
        name="read_file",
        description="Read a file from disk.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    )
    reg.register_capture_tool(
        name="submit_answer",
        description="Submit final text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    def _echo(**args) -> dict:
        return {"echoed": dict(args)}

    reg.register(
        fn=_echo, name="echo",
        description="No-required-args echo for testing.",
        parameters={"type": "object", "properties": {}},
    )
    return reg


class TestListTools:
    def test_returns_only_allowed_subset(self, reg_with_tools):
        list_td, _ = build_meta_tools(reg_with_tools,
                                       allowed={"read_file", "submit_answer"})
        out = list_td.handler()
        items = json.loads(out)
        names = {i["name"] for i in items}
        assert names == {"read_file", "submit_answer"}
        # `echo` is in registry but NOT in allowed → excluded.
        assert "echo" not in names

    def test_query_substring_filters(self, reg_with_tools):
        list_td, _ = build_meta_tools(reg_with_tools,
                                       allowed={"read_file", "submit_answer", "echo"})
        items = json.loads(list_td.handler(query="read"))
        assert [i["name"] for i in items] == ["read_file"]

    def test_unknown_name_in_allowed_silently_skipped(self, reg_with_tools):
        list_td, _ = build_meta_tools(reg_with_tools,
                                       allowed={"read_file", "vaporware"})
        items = json.loads(list_td.handler())
        # `vaporware` isn't in registry — list_tools just skips it
        # rather than crashing. call_tool will give a clean error if
        # the LLM tries to invoke it.
        names = {i["name"] for i in items}
        assert names == {"read_file"}

    def test_each_item_carries_full_schema(self, reg_with_tools):
        list_td, _ = build_meta_tools(reg_with_tools, allowed={"read_file"})
        items = json.loads(list_td.handler())
        assert items[0]["parameters"]["required"] == ["path"]


class TestCallTool:
    def test_dispatches_to_underlying(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file", args={"path": "foo.txt"})
        assert out == {"path": "foo.txt", "from": 1, "content": "<elided>"}

    def test_capture_tool_echoes_through(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"submit_answer"})
        out = call_td.handler(name="submit_answer",
                              args={"text": "all good"})
        assert out == {"status": "received", "args": {"text": "all good"}}

    def test_tool_not_in_allowed_rejected(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="echo", args={})
        assert out["error"] == "tool_not_allowed"

    def test_missing_required_arg_rejected(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file", args={"start_line": 1})
        assert out["error"] == "missing_required_args"
        assert "path" in out["detail"]
        # Schema included so the LLM can self-correct.
        assert out["schema"]["required"] == ["path"]

    def test_args_as_json_string_accepted(self, reg_with_tools):
        """Some LLMs serialise nested objects — accept both."""
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file",
                              args='{"path": "x.txt", "start_line": 5}')
        assert out["path"] == "x.txt" and out["from"] == 5

    def test_args_invalid_json_string_rejected(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file", args="{not json")
        assert out["error"] == "args_not_json"

    def test_args_not_object_rejected(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file", args=[1, 2])
        assert out["error"] == "args_not_object"

    def test_signature_mismatch_returns_structured_error(self, reg_with_tools):
        _, call_td = build_meta_tools(reg_with_tools,
                                       allowed={"read_file"})
        out = call_td.handler(name="read_file",
                              args={"path": "x", "unexpected": 42})
        assert out["error"] == "args_signature_mismatch"


class TestSchemaStability:
    def test_meta_schemas_dont_depend_on_allowed_set(self, reg_with_tools):
        """Stability of the LLM-visible tool schemas is THE point of
        meta dispatch — different `allowed` sets must yield byte-
        identical list_tools/call_tool definitions so the prompt
        cache stays warm across runs."""
        list_a, call_a = build_meta_tools(reg_with_tools, allowed={"read_file"})
        list_b, call_b = build_meta_tools(reg_with_tools,
                                           allowed={"submit_answer", "echo"})
        assert list_a.parameters == list_b.parameters
        assert call_a.parameters == call_b.parameters
        assert list_a.description == list_b.description
        assert call_a.description == call_b.description
