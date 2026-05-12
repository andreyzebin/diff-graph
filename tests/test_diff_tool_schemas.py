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


@pytest.fixture
def multi_file_repo():
    """Two-commit repo with several modified + several unchanged
    files. Used to exercise `changes_only` filtering and pagination
    (`start`/`n`) on a realistic mix where the cap can hide changes."""
    repo = tempfile.mkdtemp(prefix="multi-")
    _git(["init"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    # Base: 6 unchanged + 6 to-be-modified files. Unchanged files
    # sort alphabetically BEFORE the modified ones (`uctx_*` < `mod_*`
    # is FALSE — we picked `aaa_*` for context and `zzz_*` for changes
    # so the modified files land at the END of an alphabetical listing,
    # mirroring the mediaplanner symptom where M/A/D rows fall past
    # the truncation cap).
    for i in range(6):
        (Path(repo) / f"aaa_ctx_{i}.txt").write_text(f"context {i}\n")
    for i in range(6):
        (Path(repo) / f"zzz_mod_{i}.txt").write_text(f"original {i}\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "base"], repo)
    base = _git(["rev-parse", "HEAD"], repo)
    # Modify the zzz_* files
    for i in range(6):
        (Path(repo) / f"zzz_mod_{i}.txt").write_text(f"changed {i}\nadded line\n")
    _git(["add", "."], repo)
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


class TestDiffListFilesChangesOnly:
    """`changes_only` default = true so the agent's "show me what
    changed" intent is the default behaviour. Unchanged context files
    appear only when explicitly requested. This is what closes the
    mediaplanner-style symptom where M/A/D rows fell past the
    truncation cap and the agent (rationally) started grep'ing for
    `^+`/`^M` from desperation."""

    def test_default_excludes_unchanged(self, multi_file_repo):
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {})
        # All 6 unchanged context files must be hidden by default.
        for i in range(6):
            assert f"aaa_ctx_{i}.txt" not in out, (
                f"aaa_ctx_{i}.txt leaked into default output — "
                f"changes_only=true should hide unchanged files.\n"
                f"output:\n{out}"
            )
        # All 6 modified files must be present.
        for i in range(6):
            assert f"zzz_mod_{i}.txt" in out

    def test_changes_only_false_includes_context(self, multi_file_repo):
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch(
            "diff_list_files", {"changes_only": False}
        )
        # Both context and modified files appear.
        assert "aaa_ctx_0.txt" in out
        assert "zzz_mod_0.txt" in out

    def test_changes_only_in_schema(self, multi_file_repo):
        """The flag must be exposed in the tool schema so models
        can discover and use it."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_list_files")
        assert "changes_only" in schema["properties"]
        # Still not required — has a default.
        assert "changes_only" not in (schema.get("required") or [])


class TestDiffListFilesPagination:
    """`start` + `n` pagination so callers can scroll past the page
    cap. Footer announces the total so the model knows whether to
    paginate."""

    def test_pagination_in_schema(self, multi_file_repo):
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        schema = _schema(reg, "diff_list_files")
        assert "start" in schema["properties"]
        assert "n" in schema["properties"]
        # Neither is required (defaults handle it).
        required = schema.get("required") or []
        assert "start" not in required
        assert "n" not in required

    def test_default_page_covers_small_listing(self, multi_file_repo):
        """When everything fits on one page (< n rows), the footer
        is suppressed and the caller sees the whole list cleanly."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {})  # 6 changed files, default n=50
        assert "[showing" not in out, (
            f"single-page listing should not show pagination footer; got:\n{out}"
        )

    def test_paginates_when_more_than_page(self, multi_file_repo):
        """6 changed files but n=2 → first page = 2, footer announces
        the total and the `start` for the next page."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {"n": 2})
        # First two zzz_mod_* files present.
        assert "zzz_mod_0.txt" in out
        assert "zzz_mod_1.txt" in out
        # Files after the page are NOT present.
        assert "zzz_mod_2.txt" not in out
        # Footer points at the next page.
        assert "of 6" in out
        assert "start=2" in out

    def test_next_page(self, multi_file_repo):
        """start=2, n=2 returns rows 2..3."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {"start": 2, "n": 2})
        assert "zzz_mod_2.txt" in out
        assert "zzz_mod_3.txt" in out
        # Rows outside this page absent.
        assert "zzz_mod_0.txt" not in out
        assert "zzz_mod_1.txt" not in out
        assert "zzz_mod_4.txt" not in out
        # Footer still shown (pagination is active even mid-listing).
        assert "of 6" in out

    def test_last_page_still_shows_footer_when_started_past_zero(
        self, multi_file_repo,
    ):
        """Even if a request covers the tail of the listing, the
        footer should still surface — otherwise the caller wouldn't
        know whether more rows exist past their window."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        out = reg.dispatch("diff_list_files", {"start": 4, "n": 10})
        # Last two files are visible.
        assert "zzz_mod_4.txt" in out
        assert "zzz_mod_5.txt" in out
        # Footer announces position (start>0 → footer even if no next page).
        assert "of 6" in out

    def test_pagination_with_changes_only_false(self, multi_file_repo):
        """Pagination respects the changes_only filter — `total` counts
        rows AFTER status filter, not before."""
        repo, base, source = multi_file_repo
        _, reg = _ctx_and_registry(repo, base, source)
        # 12 total files (6 ctx + 6 mod), n=5 → footer says "of 12".
        out = reg.dispatch(
            "diff_list_files",
            {"changes_only": False, "n": 5},
        )
        assert "of 12" in out
        # 6 changed files, n=5, changes_only default true → footer says "of 6".
        out2 = reg.dispatch("diff_list_files", {"n": 5})
        assert "of 6" in out2
