"""Compile-time merge of `data:` + `guards:` across system.md and user.md
frontmatter — the methodology/interface split.

system.md is the agent identity (closed for modification); user.md is the
interface contract (the concrete invocation surface). `data:` and `guards:`
declared in either layer merge into a single `AgentRegistryEntry`; a key
that lives in both layers is a hard error so a rename can't silently shadow
the other side.
"""
from __future__ import annotations

import pathlib
import pytest

from orchestra import compile_prompts


def _write_pair(tmp_path: pathlib.Path, agent: str,
                system_body: str = "", user_body: str = "") -> None:
    (tmp_path / f"{agent}.system.md").write_text(system_body)
    (tmp_path / f"{agent}.user.md").write_text(user_body)


def _registry(tmp_path: pathlib.Path):
    return compile_prompts(str(tmp_path))


class TestDataMerge:
    def test_user_layer_only(self, tmp_path):
        """Methodology + interface fully split: no `data:` in system."""
        _write_pair(tmp_path, "reviewer",
            system_body=(
                "---\n"
                "agent: reviewer\n"
                "mode: react\n"
                "tools: [done]\n"
                "---\n"
                "methodology body\n"
            ),
            user_body=(
                "---\n"
                "data:\n"
                "  pr_title: {type: string}\n"
                "  commits: {type: string, from: pr_context.commits}\n"
                "---\n"
                "task body\n"
            ),
        )
        e = _registry(tmp_path).get("reviewer")
        assert set(e.input_schema.keys()) == {"pr_title", "commits"}
        # `from:` parses into from_tool + from_field
        assert e.input_schema["commits"]["from_tool"] == "pr_context"
        assert e.input_schema["commits"]["from_field"] == "commits"

    def test_split_across_layers(self, tmp_path):
        """Framework-infra in system, interface in user — merged into one schema."""
        _write_pair(tmp_path, "dispatcher",
            system_body=(
                "---\n"
                "agent: dispatcher\n"
                "mode: react\n"
                "tools: [done]\n"
                "data:\n"
                "  generation: {type: string}\n"
                "  mutation:   {type: string}\n"
                "---\n"
                "body\n"
            ),
            user_body=(
                "---\n"
                "data:\n"
                "  message:    {type: string}\n"
                "  comment_id: {type: integer}\n"
                "---\n"
                "task\n"
            ),
        )
        e = _registry(tmp_path).get("dispatcher")
        assert set(e.input_schema.keys()) == {
            "generation", "mutation", "message", "comment_id",
        }
        assert e.input_schema["comment_id"]["type"] == "integer"

    def test_conflict_raises(self, tmp_path, caplog):
        """Same field in both layers → compile error (logged at WARNING by
        compile_prompts's per-file try/except; the agent does not register)."""
        _write_pair(tmp_path, "x",
            system_body=(
                "---\n"
                "agent: x\n"
                "mode: react\n"
                "tools: [done]\n"
                "data:\n"
                "  pr_title: {type: string}\n"
                "---\n"
                "body\n"
            ),
            user_body=(
                "---\n"
                "data:\n"
                "  pr_title: {type: string}\n"
                "---\n"
                "task\n"
            ),
        )
        registry = _registry(tmp_path)
        # Agent didn't register — compiler bailed on the conflict.
        assert "x" not in registry.names()
        # And the reason surfaced in the warning log.
        assert any(
            "declared in both system.md and user.md" in rec.message
            and "pr_title" in rec.message
            for rec in caplog.records
        )

    def test_user_data_only_no_system_data(self, tmp_path):
        """System.md without any `data:` is the common case after the
        methodology/interface split — interface data lives entirely in
        user.md."""
        _write_pair(tmp_path, "investigator",
            system_body=(
                "---\n"
                "agent: investigator\n"
                "mode: react\n"
                "tools: [done]\n"
                "---\n"
                "investigation methodology\n"
            ),
            user_body=(
                "---\n"
                "data:\n"
                "  focus: {type: string, description: \"what to investigate\"}\n"
                "---\n"
                "task\n"
            ),
        )
        e = _registry(tmp_path).get("investigator")
        assert e.input_schema == {
            "focus": {"type": "string", "description": "what to investigate"},
        }


class TestGuardsMerge:
    def test_methodology_guard_in_system(self, tmp_path):
        _write_pair(tmp_path, "x",
            system_body=(
                "---\n"
                "agent: x\n"
                "mode: react\n"
                "tools: [done]\n"
                "guards:\n"
                "  text_response: \"use tools\"\n"
                "---\n"
                "body\n"
            ),
            user_body="task\n",
        )
        e = _registry(tmp_path).get("x")
        assert e.guards == {"text_response": "use tools"}

    def test_interface_guard_in_user(self, tmp_path):
        _write_pair(tmp_path, "x",
            system_body=(
                "---\n"
                "agent: x\n"
                "mode: react\n"
                "tools: [done]\n"
                "---\n"
                "body\n"
            ),
            user_body=(
                "---\n"
                "guards:\n"
                "  require_tool:post_comment: \"post first\"\n"
                "---\n"
                "task\n"
            ),
        )
        e = _registry(tmp_path).get("x")
        assert e.guards == {"require_tool:post_comment": "post first"}

    def test_merge_across_layers(self, tmp_path):
        _write_pair(tmp_path, "x",
            system_body=(
                "---\n"
                "agent: x\n"
                "mode: react\n"
                "tools: [done]\n"
                "guards:\n"
                "  text_response: \"use tools\"\n"
                "---\n"
                "body\n"
            ),
            user_body=(
                "---\n"
                "guards:\n"
                "  require_tool:post_comment: \"post first\"\n"
                "---\n"
                "task\n"
            ),
        )
        e = _registry(tmp_path).get("x")
        assert e.guards == {
            "text_response": "use tools",
            "require_tool:post_comment": "post first",
        }

    def test_conflict_raises(self, tmp_path, caplog):
        _write_pair(tmp_path, "x",
            system_body=(
                "---\n"
                "agent: x\n"
                "mode: react\n"
                "tools: [done]\n"
                "guards:\n"
                "  text_response: \"system message\"\n"
                "---\n"
                "body\n"
            ),
            user_body=(
                "---\n"
                "guards:\n"
                "  text_response: \"user message\"\n"
                "---\n"
                "task\n"
            ),
        )
        registry = _registry(tmp_path)
        assert "x" not in registry.names()
        assert any(
            "`guards:` triggers declared in both" in rec.message
            and "text_response" in rec.message
            for rec in caplog.records
        )
