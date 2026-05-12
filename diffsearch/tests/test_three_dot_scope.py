"""VFS uses three-dot diff semantics — `base...source` (changes
the source branch contributed), not two-dot `base..source` (anything
that differs between the two refs, including base's evolution after
the fork point).

Repro: master deletes `docs/ai-stand-11.md` after the feature branch
is cut. On the SBLOOM-138 PR the agent saw the file as `(deleted)`
even though the PR itself never touched it. Root cause: VFS
materialisation ran `git diff --name-status BASE SOURCE` (two-dot),
which sees master's deletion. Three-dot anchors at the merge-base,
so only the PR's actual contribution surfaces.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from diffsearch.virtual_fs import (
    build_virtual_file,
    get_changed_files,
    get_path_status,
    materialize_vfs,
)
from diffsearch.tools import list_files_vfs


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def base_advanced_after_fork():
    """Build a repo where:

      M0 — initial commit (master, also fork point of feature).
           Has docs/stale.md (the future "phantom deleted" file)
           plus other files.
      M1 — master advances by deleting docs/stale.md (and only that).
      F1 — feature branch (cut at M0) adds src/Feature.java.

    Returns `(repo, base, source)` where `base=M1` (current master),
    `source=F1` (PR's tip).

    What we expect VFS to see for `git diff base...source` (three-dot
    against merge-base = M0):
      A src/Feature.java  ← the PR's only contribution.

    What two-dot would (wrongly) see:
      A src/Feature.java
      A docs/stale.md     ← phantom: feature has it because it was at
                            M0; master deleted it; two-dot calls that
                            an "addition" by feature, which is misleading.
    """
    repo = tempfile.mkdtemp(prefix="three-dot-")
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "master"], repo)
    p = Path(repo)

    # M0: initial commit on master.
    (p / "docs").mkdir()
    (p / "src").mkdir()
    (p / "docs" / "stale.md").write_text("doomed by master, untouched by feature\n")
    (p / "src" / "Existing.java").write_text("class Existing {}\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "M0"], repo)
    m0 = _git(["rev-parse", "HEAD"], repo)

    # Fork feature off M0 BEFORE master advances.
    _git(["checkout", "-q", "-b", "feature", m0], repo)
    (p / "src" / "Feature.java").write_text("class Feature {}\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "F1"], repo)
    f1 = _git(["rev-parse", "HEAD"], repo)

    # Master advances — deletes docs/stale.md.
    _git(["checkout", "-q", "master"], repo)
    _git(["rm", "-q", "docs/stale.md"], repo)
    _git(["commit", "-qm", "M1: drop stale doc"], repo)
    m1 = _git(["rev-parse", "HEAD"], repo)

    yield repo, m1, f1
    shutil.rmtree(repo, ignore_errors=True)


class TestThreeDotScope:

    def test_get_changed_files_returns_only_source_contribution(
        self, base_advanced_after_fork,
    ):
        repo, base, source = base_advanced_after_fork
        files = get_changed_files(base, source, repo)
        assert files == ["src/Feature.java"], (
            f"three-dot must only show the PR's contribution, "
            f"not master's deletion of docs/stale.md.\nGot: {files}"
        )

    def test_get_path_status_excludes_base_only_changes(
        self, base_advanced_after_fork,
    ):
        repo, base, source = base_advanced_after_fork
        status = get_path_status(base, source, repo)
        assert status == {"src/Feature.java": "A"}, (
            f"docs/stale.md must NOT appear with a D marker — it was "
            f"deleted by master, not by the PR.\nGot: {status}"
        )

    def test_build_virtual_file_skips_master_deletion(
        self, base_advanced_after_fork,
    ):
        """`build_virtual_file('docs/stale.md', ...)` against three-dot
        scope finds no diff (PR didn't touch it) and falls back to
        reading the plain source-tree version. The file CONTENT is
        present in feature's tree, so we read it as unchanged
        context — no `-`-marker phantom deletion."""
        repo, base, source = base_advanced_after_fork
        vf = build_virtual_file(base, source, "docs/stale.md", repo)
        # Plain-source fallback path: all lines are unchanged context
        # (marker == " "), not deletions (marker == "-").
        markers = {line.marker for line in vf.lines}
        assert markers <= {" "}, (
            f"docs/stale.md should appear as unchanged context "
            f"under three-dot scope (PR didn't touch it). Got "
            f"markers: {markers}"
        )

    def test_materialize_vfs_excludes_phantom_deletion(
        self, base_advanced_after_fork,
    ):
        """End-to-end: the VFS's status index lists only the PR's
        contributed file. `list_files_vfs(changes_only=true)` then
        surfaces nothing about docs/stale.md to the agent."""
        repo, base, source = base_advanced_after_fork
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs, changes_only=True)
            # Phantom file MUST NOT appear with any status marker.
            assert "docs/stale.md" not in out, (
                f"phantom-deleted file leaked into changes_only "
                f"listing:\n{out}"
            )
            # PR's real change IS surfaced.
            assert "src/Feature.java" in out
            # And it carries the A marker.
            for line in out.splitlines():
                if "src/Feature.java" in line:
                    assert line.lstrip().startswith("A"), (
                        f"Feature.java should have A marker, got: {line!r}"
                    )
                    break
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
