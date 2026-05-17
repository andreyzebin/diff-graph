"""Tests for benchmarks.runner.invocations_check — the structural
assertion layer that complements the LLM judge for tests where the
call pattern is the whole point (SKILL-001, future invocation-shape
fixtures).
"""
from __future__ import annotations

import json
from pathlib import Path

from benchmarks.runner.invocations_check import (
    Violation,
    check,
    format_violations,
    load_invocations,
)


def _inv(agent: str, tool: str) -> dict:
    return {"agent": agent, "step": 0, "tool": tool, "args": {}, "mocked": False, "mock_when": None}


def test_empty_spec_returns_no_violations():
    invs = [_inv("boss", "agent_spawn")]
    assert check(invs, {}) == []
    assert check(invs, None or {}) == []


def test_must_call_present_passes():
    invs = [_inv("boss", "agent_spawn"), _inv("worker", "do_task")]
    spec = {"boss": {"must_call": ["agent_spawn"]}}
    assert check(invs, spec) == []


def test_must_call_missing_fails():
    invs = [_inv("boss", "do_task")]
    spec = {"boss": {"must_call": ["agent_spawn"]}}
    v = check(invs, spec)
    assert len(v) == 1
    assert v[0].kind == "missing_call"
    assert v[0].tool == "agent_spawn"
    assert "do_task" in v[0].detail


def test_must_not_call_absent_passes():
    invs = [_inv("boss", "agent_spawn")]
    spec = {"boss": {"must_not_call": ["do_task"]}}
    assert check(invs, spec) == []


def test_must_not_call_present_fails_with_count():
    invs = [_inv("boss", "do_task"), _inv("boss", "do_task"), _inv("boss", "agent_spawn")]
    spec = {"boss": {"must_not_call": ["do_task"]}}
    v = check(invs, spec)
    assert len(v) == 1
    assert v[0].kind == "forbidden_call"
    assert v[0].tool == "do_task"
    assert "saw 2" in v[0].detail


def test_agent_never_ran_fails_must_call_passes_must_not():
    invs: list[dict] = []
    spec = {
        "boss": {"must_call": ["agent_spawn"], "must_not_call": ["do_task"]},
    }
    v = check(invs, spec)
    assert len(v) == 1
    assert v[0].kind == "missing_call"


def test_unrelated_agents_ignored():
    invs = [_inv("investigator", "diff_read_file")]
    spec = {"boss": {"must_call": ["agent_spawn"]}}
    v = check(invs, spec)
    # Only `boss` is in the spec — investigator's calls don't matter.
    assert len(v) == 1
    assert v[0].agent == "boss"


def test_skill_001_with_pattern():
    """The real SKILL-001-with shape: boss delegates, worker executes."""
    invs = [
        _inv("boss", "agent_spawn"),
        _inv("worker", "do_task"),
        _inv("boss", "text_answer"),
    ]
    spec = {
        "boss":   {"must_call": ["agent_spawn"], "must_not_call": ["do_task"]},
        "worker": {"must_call": ["do_task"]},
    }
    assert check(invs, spec) == []


def test_skill_001_with_pattern_when_boss_skipped_delegation():
    """Failure case: boss has the skill but does the work himself."""
    invs = [
        _inv("boss", "do_task"),
        _inv("boss", "text_answer"),
    ]
    spec = {
        "boss":   {"must_call": ["agent_spawn"], "must_not_call": ["do_task"]},
        "worker": {"must_call": ["do_task"]},
    }
    v = check(invs, spec)
    kinds = {(vio.agent, vio.kind, vio.tool) for vio in v}
    assert ("boss", "missing_call", "agent_spawn") in kinds
    assert ("boss", "forbidden_call", "do_task") in kinds
    assert ("worker", "missing_call", "do_task") in kinds


def test_load_invocations_missing_file(tmp_path: Path):
    assert load_invocations(tmp_path / "nope.json") == []


def test_load_invocations_malformed(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert load_invocations(p) == []


def test_load_invocations_happy_path(tmp_path: Path):
    p = tmp_path / "invs.json"
    p.write_text(json.dumps({"invocations": [_inv("boss", "agent_spawn")]}), encoding="utf-8")
    assert load_invocations(p) == [_inv("boss", "agent_spawn")]


def test_format_violations_empty_string_when_none():
    assert format_violations([]) == ""


def test_format_violations_formats_with_header():
    v = [Violation(agent="boss", kind="missing_call", tool="agent_spawn", detail="expected at least one")]
    out = format_violations(v)
    assert "assert_invocations violations:" in out
    assert "[boss] missing_call: agent_spawn" in out
