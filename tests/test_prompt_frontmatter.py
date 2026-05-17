"""Tests for orchestra.prompts.frontmatter + register_capture_tool.

Covers (a) the basic split, (b) the strict validation of known fields,
(c) the no-frontmatter happy path, (d) error cases that should fail
loud rather than fall back to defaults, (e) capture-tool handler
records args and echoes back.
"""
from __future__ import annotations

import pytest

from orchestra.prompts.frontmatter import parse, validate, Frontmatter
from orchestra.tools.registry import ToolRegistry


class TestParse:
    def test_no_frontmatter_passes_through(self):
        text = "PR: #123\n\nReview this PR.\n"
        fm = parse(text)
        assert fm.meta == {}
        assert fm.body == text

    def test_full_frontmatter_parsed(self):
        text = (
            "---\n"
            "dispatch_mode: native\n"
            "tools: [diff_read_file, reflect, done]\n"
            "---\n"
            "PR: foo\n"
            "\n"
            "Review this PR.\n"
        )
        fm = parse(text)
        assert fm.meta["dispatch_mode"] == "native"
        assert fm.meta["tools"] == ["diff_read_file", "reflect", "done"]
        assert fm.body.startswith("PR: foo")
        # No frontmatter fence in body — only the actual content.
        assert "---" not in fm.body

    def test_empty_frontmatter_is_legal(self):
        text = "---\n---\nHello.\n"
        fm = parse(text)
        assert fm.meta == {}
        assert fm.body.strip() == "Hello."

    def test_leading_bom_tolerated(self):
        text = "﻿---\ntools: [reflect]\n---\nbody\n"
        fm = parse(text)
        assert fm.meta["tools"] == ["reflect"]
        assert fm.body.strip() == "body"

    def test_extra_tools_capture_spec(self):
        text = (
            "---\n"
            "extra_tools:\n"
            "  - name: submit_answer\n"
            "    description: 'Submit text'\n"
            "    parameters:\n"
            "      type: object\n"
            "      properties:\n"
            "        text: {type: string}\n"
            "      required: [text]\n"
            "---\n"
            "Body\n"
        )
        fm = parse(text)
        assert len(fm.meta["extra_tools"]) == 1
        spec = fm.meta["extra_tools"][0]
        assert spec["name"] == "submit_answer"
        assert spec["parameters"]["required"] == ["text"]

    def test_missing_close_fence_raises(self):
        # No closing --- after the YAML — strict failure so prompt
        # authors notice immediately, no silent fallback.
        with pytest.raises(ValueError, match="no matching close"):
            parse("---\ntools: [reflect]\n\nBody without close.\n")

    def test_malformed_yaml_raises(self):
        with pytest.raises(ValueError, match="invalid YAML"):
            parse("---\ntools: [unclosed\n---\nbody\n")

    def test_non_mapping_top_level_raises(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            parse("---\n- just_a_list\n- of_items\n---\nbody\n")


class TestValidate:
    def test_empty_passes(self):
        validate(Frontmatter(meta={}, body=""))   # no fields, no error

    def test_dispatch_mode_must_be_native_or_meta(self):
        with pytest.raises(ValueError, match="dispatch_mode"):
            validate(Frontmatter(meta={"dispatch_mode": "weird"}))

    def test_dispatch_mode_native_accepted(self):
        validate(Frontmatter(meta={"dispatch_mode": "native"}))
        validate(Frontmatter(meta={"dispatch_mode": "meta"}))

    def test_tools_must_be_list_of_nonempty_strings(self):
        with pytest.raises(ValueError, match=r"tools: "):
            validate(Frontmatter(meta={"tools": "diff_read_file"}))
        with pytest.raises(ValueError, match=r"tools\[0\]"):
            validate(Frontmatter(meta={"tools": [""]}))
        with pytest.raises(ValueError, match=r"tools\[0\]"):
            validate(Frontmatter(meta={"tools": [123]}))

    def test_extra_tools_must_carry_required_fields(self):
        with pytest.raises(ValueError, match="missing required field 'description'"):
            validate(Frontmatter(meta={
                "extra_tools": [{"name": "x", "parameters": {}}]
            }))
        with pytest.raises(ValueError, match="missing required field 'parameters'"):
            validate(Frontmatter(meta={
                "extra_tools": [{"name": "x", "description": "d"}]
            }))
        with pytest.raises(ValueError, match="parameters: must be"):
            validate(Frontmatter(meta={
                "extra_tools": [
                    {"name": "x", "description": "d", "parameters": "not-a-map"}
                ]
            }))

    def test_well_formed_extra_tool_accepted(self):
        validate(Frontmatter(meta={
            "extra_tools": [{
                "name": "submit_answer",
                "description": "Submit your text answer.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }],
        }))

    def test_unknown_fields_pass(self):
        # Forward-compat: future fields can land without us updating
        # this validator; the contract is only "known fields have
        # well-formed shapes".
        validate(Frontmatter(meta={"future_field": "anything"}))

    def test_user_role_allows_tools(self):
        """`tools:` in the user (per-run) layer is now additive —
        merged into the agent's base tools at _build_tool_names.
        No silent-replace risk because there's no replace mode
        anymore. Pinned: validator accepts it at any layer."""
        validate(Frontmatter(meta={"tools": ["text_answer"]}), role="user")

    def test_system_role_still_allows_tools(self):
        # System layer = agent's own prompt; same `tools:` field
        # (additive starting from empty for system, additive on
        # top of system for user). No layer-specific restriction.
        validate(Frontmatter(meta={"tools": ["diff_read_file"]}), role="system")
        validate(Frontmatter(meta={"tools": ["diff_read_file"]}))  # role=None


class TestCaptureTool:
    def test_handler_echoes_args(self):
        reg = ToolRegistry()
        td = reg.register_capture_tool(
            name="submit_answer",
            description="Submit your final text.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
        # Returned ToolDef is the same one stored in the registry.
        assert reg.has("submit_answer")
        assert reg.get("submit_answer") is td
        # Handler echoes args back so the judge can read what was
        # submitted from the tool_response event.
        out = td.handler(text="LGTM, no concerns.")
        assert out == {"status": "received", "args": {"text": "LGTM, no concerns."}}

    def test_register_overwrites(self):
        reg = ToolRegistry()
        reg.register_capture_tool(name="t", description="v1", parameters={"type": "object"})
        reg.register_capture_tool(name="t", description="v2", parameters={"type": "object"})
        assert reg.get("t").description == "v2"

    def test_schema_makes_it_into_openai_format(self):
        reg = ToolRegistry()
        reg.register_capture_tool(
            name="submit_answer",
            description="Submit",
            parameters={"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
        )
        schemas = reg.to_openai_schema(["submit_answer"])
        assert len(schemas) == 1
        fn = schemas[0]["function"]
        assert fn["name"] == "submit_answer"
        assert fn["parameters"]["required"] == ["text"]
