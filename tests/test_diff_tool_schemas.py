"""
Tool-schema contracts for diff_*.

These pin which arguments are actually required for the agent
to dispatch a tool. The fields that have Python-side defaults
must NOT show up in the JSONSchema `required` list — otherwise
agents that omit them (the typical opening call
`diff_list_files()` for orientation) trip
`validation error: '<field>' is a required property` BEFORE
their handler runs. That's what bit plan 107's reviewer.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def tiny_repo_with_diff():
    """Two-commit repo so `base..source` produces a real diff and
    `diff_list_files` has something to list."""
    repo = tempfile.mkdtemp(prefix="schema-")
    _git(["init"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (Path(repo) / "a.txt").write_text("hello\n")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-m", "base"], repo)
    base = _git(["rev-parse", "HEAD"], repo)
    (Path(repo) / "a.txt").write_text("hello\nworld\n")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-m", "source"], repo)
    source = _git(["rev-parse", "HEAD"], repo)
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


def _ctx_and_registry(repo: str, base: str, source: str):
    from diffgraph.orchestrator import _Ctx, ReviewContext
    from diffgraph.diff_parser import DiffResult
    from diffgraph.orchestra_tools import register_diffgraph_tools
    from orchestra import ToolRegistry

    diff = subprocess.run(
        ["git", "diff", f"{base}..{source}"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    ctx = _Ctx(
        diff_text=diff,
        diff_result=DiffResult(files={}, changed_files=[], changed_lines={}),
        repo_path=repo, existing_comments=[], review_context=ReviewContext(),
        base_ref=base, source_ref=source,
        _pr_url="", _initialized=True,
    )
    reg = ToolRegistry()
    register_diffgraph_tools(reg, ctx)
    return ctx, reg


def _schema(reg, name: str) -> dict:
    """Pull the JSONSchema the tool registers under the LLM-facing
    `parameters` slot. The registry stores tools keyed by name; we
    poke at the registered descriptor."""
    tool = reg._tools[name]
    return tool.parameters


class TestDiffListFilesSchema:
    def test_no_required_args(self, tiny_repo_with_diff):
        """Both `pattern` and `ref` have Python defaults; neither
        belongs in `required`. Reviewers consistently open with a
        no-arg `diff_list_files()` to orient — that call MUST not
        fail schema validation."""
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_list_files")
        assert "pattern" in schema["properties"]
        assert "ref" in schema["properties"]
        assert schema.get("required") == [], \
            f"diff_list_files schema should have no required args; got {schema.get('required')!r}"

    def test_dispatch_with_no_args(self, tiny_repo_with_diff):
        """End-to-end: dispatch with `{}` returns content, not a
        validation error string."""
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {})
        assert "validation error" not in out.lower(), \
            f"empty-args dispatch tripped schema validation: {out}"
        assert "a.txt" in out

    def test_dispatch_with_pattern_only(self, tiny_repo_with_diff):
        """Caller supplies pattern but omits ref — still must work
        (ref defaults to `base..source` when ctx has both refs)."""
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {"pattern": "**/*.txt"})
        assert "validation error" not in out.lower()
        assert "a.txt" in out


class TestOtherDiffTools:
    """The other diff_* tools genuinely need their core argument,
    so `required` IS justified for them. This guards against
    accidentally over-relaxing them when fixing diff_list_files."""

    def test_diff_read_file_requires_path(self, tiny_repo_with_diff):
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_read_file")
        assert schema.get("required") == ["path"]

    def test_diff_outline_requires_path(self, tiny_repo_with_diff):
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_outline")
        assert schema.get("required") == ["path"]

    def test_diff_search_requires_query(self, tiny_repo_with_diff):
        repo, base, source = tiny_repo_with_diff
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_search")
        assert schema.get("required") == ["query"]
