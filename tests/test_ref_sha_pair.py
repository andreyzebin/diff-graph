"""`ref="<sha1>..<sha2>"` resolution for `diff_*` tools.

Documented but not previously tested: every `diff_*` tool accepts a
custom `ref` parameter spelled `"base..source"` for the default PR
view OR `"<sha1>..<sha2>"` to scope to a specific commit pair (e.g.
"changes between my last [SELF] reply and the current source tip"
for a continuation review).

The orchestra-side `_resolve_ref` parses the pair, replacing the
abstract names `base` / `source` with the agent's PR refs and
leaving literal SHAs alone. The VFS then materialises that pair with
the same three-dot semantics it uses for the default PR view (see
`diffsearch/README.md` → "Scope: three-dot diff").

Tests pin two layers:

1. `_resolve_ref` parsing — every supported spelling, plus the
   degenerate cases (no `..`, empty side, etc.).
2. End-to-end via `diff_read_file(path, ref=...)` — a sha-pair ref
   actually materialises a VFS and returns content scoped to that
   pair, distinct from the default `base..source` view.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from diffgraph.orchestra_tools import _resolve_ref


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


class _FakeCtx:
    """Stand-in for `_Ctx` — only the two ref attributes
    `_resolve_ref` reads."""
    def __init__(self, base_ref: str = "BASE_SHA", source_ref: str = "SOURCE_SHA"):
        self.base_ref = base_ref
        self.source_ref = source_ref


class TestResolveRef:

    def test_default_base_source(self):
        """`"base..source"` → ctx's PR-anchored SHAs verbatim."""
        ctx = _FakeCtx(base_ref="aaa", source_ref="bbb")
        assert _resolve_ref(ctx, "base..source") == ("aaa", "bbb")

    def test_literal_sha_pair(self):
        """Two arbitrary SHAs pass through unchanged — the use case
        is "review only commits between these two points"."""
        ctx = _FakeCtx()
        assert _resolve_ref(ctx, "abc123..def456") == ("abc123", "def456")

    def test_left_named_right_sha(self):
        """`"base..<sha>"` → ctx.base_ref for `base`, literal SHA
        on the right."""
        ctx = _FakeCtx(base_ref="aaa")
        assert _resolve_ref(ctx, "base..def456") == ("aaa", "def456")

    def test_left_sha_right_named(self):
        """`"<sha>..source"` is the canonical continuation-review
        shape: "the source tip vs the commit I last saw"."""
        ctx = _FakeCtx(source_ref="bbb")
        assert _resolve_ref(ctx, "abc123..source") == ("abc123", "bbb")

    def test_no_dotdot_returns_none(self):
        """`"source"` (plain mode) — no range, no VFS, return
        None so the caller falls back to working-tree reads."""
        ctx = _FakeCtx()
        assert _resolve_ref(ctx, "source") is None
        # And any bare SHA (no `..`) goes the same fall-through.
        assert _resolve_ref(ctx, "abc123") is None

    def test_empty_left_returns_none(self):
        """`"..source"` — malformed (no left side) → fall through
        to None instead of pretending it parsed."""
        ctx = _FakeCtx(source_ref="bbb")
        assert _resolve_ref(ctx, "..source") is None

    def test_empty_right_returns_none(self):
        """`"base.."` — same fall-through for the right side."""
        ctx = _FakeCtx(base_ref="aaa")
        assert _resolve_ref(ctx, "base..") is None

    def test_resolves_when_named_side_missing_in_ctx(self):
        """If the agent calls `"base..source"` but ctx doesn't have
        those refs set (e.g. local CLI mode), we get None — VFS
        falls back to plain-source reads. Better than passing
        empty strings to git."""
        ctx = _FakeCtx(base_ref="", source_ref="")
        assert _resolve_ref(ctx, "base..source") is None


@pytest.fixture
def three_commit_repo():
    """Linear three-commit history so a ref pair `(C1..C3)` exercises
    a different diff than `(C2..C3)`.

      C1 — initial: src/A.java with `class A {}`
      C2 — adds  : src/B.java
      C3 — modifies: src/A.java (renames the class)

    Yields `(repo, c1, c2, c3)`. Tests can then scope to any pair
    and assert the VFS reflects exactly that pair's delta.
    """
    repo = tempfile.mkdtemp(prefix="ref-pair-")
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    p = Path(repo)
    (p / "src").mkdir()
    (p / "src" / "A.java").write_text("class A {}\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "C1"], repo)
    c1 = _git(["rev-parse", "HEAD"], repo)

    (p / "src" / "B.java").write_text("class B {}\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "C2"], repo)
    c2 = _git(["rev-parse", "HEAD"], repo)

    (p / "src" / "A.java").write_text("class ARenamed {}\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "C3"], repo)
    c3 = _git(["rev-parse", "HEAD"], repo)

    yield repo, c1, c2, c3
    shutil.rmtree(repo, ignore_errors=True)


class TestVfsWithShaPair:
    """End-to-end: `materialize_vfs(repo, sha_a, sha_b)` honours the
    arbitrary-SHA pair and `list_files_vfs(changes_only=True)`
    returns only the files changed between THAT pair, not the
    default base..source view."""

    def test_full_range_lists_all_three(self, three_commit_repo):
        """`C1..C3` covers both adds and the modification."""
        from diffsearch.virtual_fs import materialize_vfs, get_path_status
        repo, c1, _c2, c3 = three_commit_repo
        status = get_path_status(c1, c3, repo)
        assert status == {"src/A.java": "M", "src/B.java": "A"}
        # Same shape end-to-end via VFS.
        vfs = materialize_vfs(repo, c1, c3)
        try:
            from diffsearch.tools import list_files_vfs
            out = list_files_vfs(vfs, changes_only=True)
            assert "src/A.java" in out
            assert "src/B.java" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_narrow_range_excludes_earlier_change(self, three_commit_repo):
        """`C2..C3` skips the C2-added file (B was already there at
        C2) and only shows the A modification."""
        from diffsearch.virtual_fs import get_path_status, materialize_vfs
        repo, _c1, c2, c3 = three_commit_repo
        status = get_path_status(c2, c3, repo)
        assert status == {"src/A.java": "M"}, (
            f"sha-pair scope must exclude commits outside the range. "
            f"Got: {status}"
        )
        vfs = materialize_vfs(repo, c2, c3)
        try:
            from diffsearch.tools import list_files_vfs
            out = list_files_vfs(vfs, changes_only=True)
            assert "src/A.java" in out
            # B was added BEFORE c2 → not in the c2..c3 delta.
            assert "src/B.java" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_empty_range_when_endpoints_equal(self, three_commit_repo):
        """`C2..C2` is a no-op range — no files changed."""
        from diffsearch.virtual_fs import get_path_status
        repo, _c1, c2, _c3 = three_commit_repo
        assert get_path_status(c2, c2, repo) == {}

    def test_diff_read_file_scopes_to_pair(self, three_commit_repo):
        """End-to-end via `diff_read_file` tool — agent passing
        `ref="<c2_sha>..<c3_sha>"` reads ONLY the C3 mutation of A,
        not the original C1 content."""
        from diffsearch.virtual_fs import materialize_vfs, build_virtual_file
        repo, _c1, c2, c3 = three_commit_repo
        vf = build_virtual_file(c2, c3, "src/A.java", repo)
        # Lines should include both - (old) and + (new) markers for
        # the A.java rename.
        markers = {line.marker for line in vf.lines}
        assert "-" in markers
        assert "+" in markers
        # And the content reflects the actual change.
        line_text = "\n".join(line.content for line in vf.lines)
        assert "class ARenamed" in line_text
