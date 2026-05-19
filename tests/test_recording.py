"""End-to-end test for the recording capture writer (TODO §19 Phase 1).

Builds a tiny git repo with base/source branches, drives RecordingWriter
through a 3-invocation flow, then asserts:
  - pr.json written once, fields preserved
  - 3 invocations/* dirs with snapshot.json + triggered_by.json + output.json
  - manifest.json lists all 3 rev-NN entries
  - repo.bundle restorable as a real git repo
  - Restored bundle has refs/diffgraph/PR-N/{base,source,rev-01,rev-02,rev-03}
  - refs.txt matches the bundle refs
  - Jira capture lands in invocations/N/jira/
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from diffgraph.recording import (
    CommentSnapshot,
    PRSnapshot,
    RecordingWriter,
    pr_dir_for,
    stable_id_for_agent_comment,
    stable_id_for_external_comment,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Iterator[Path]:
    """Tiny upstream git repo with master (base) + feat (source) branches,
    3 commits on feat to simulate 3 source-branch revisions over the
    PR's lifetime. Author/committer identity set explicitly so the
    bundle audit can verify it later."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "alice@example.com",
           "GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com",
           "GIT_AUTHOR_DATE": "2026-05-10T12:00:00+0000",
           "GIT_COMMITTER_DATE": "2026-05-10T12:00:00+0000"}
    (repo / "OrderService.java").write_text(
        "class OrderService {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True, env=env)
    # source branch with 3 commits — these are our rev-01, rev-02, rev-03
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"],
                   check=True, env=env)
    sources = []
    for i in range(3):
        (repo / "OrderService.java").write_text(
            f"class OrderService {{ /* v{i+1} */ }}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q",
                        "-m", f"rev-{i+1:02d}"], check=True, env=env)
        sources.append(_git(repo, "rev-parse", "HEAD"))
    base_sha = _git(repo, "rev-parse", "master")
    yield repo
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def record_root(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "recordings"
    root.mkdir()
    yield root


# ── Tests ─────────────────────────────────────────────────────────────────


PR_URL = "https://bitbucket.example.com/projects/PROJ/repos/orderflow/pull-requests/1234"


def test_pr_dir_for_layout(tmp_path: Path) -> None:
    """Path scheme honours host + project + repo + PR id."""
    p = pr_dir_for(tmp_path, "https://bitbucket.example.com",
                   "PROJ", "orderflow", 1234)
    assert p == tmp_path / "bitbucket.example.com" / "PROJ" / "orderflow" / "PR-1234"


def test_stable_id_namespaces() -> None:
    """h-/a- namespaces don't collide."""
    assert stable_id_for_external_comment(42) == "c-42"
    assert stable_id_for_agent_comment(1, 1) == "a-001-01"
    assert stable_id_for_agent_comment(15, 7) == "a-015-07"


def test_open_returns_none_for_unparseable_url(record_root: Path) -> None:
    w = RecordingWriter.open(record_root, "not a real url")
    assert w is None


def test_open_returns_none_when_disk_below_floor(record_root: Path) -> None:
    # Floor of 1 PiB — guaranteed below.
    w = RecordingWriter.open(record_root, PR_URL,
                              min_free_bytes=10**18)
    assert w is None


def test_full_capture_flow(upstream_repo: Path, record_root: Path) -> None:
    """3-invocation capture against a real local git repo. Verifies
    every artefact + bundle integrity end-to-end."""
    # Resolve the 3 source SHAs the upstream fixture made (master..feat
    # excludes the init commit on master, leaving just the 3 feat commits).
    source_revs = subprocess.run(
        ["git", "-C", str(upstream_repo), "log", "--format=%H", "master..feat"],
        check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    source_revs.reverse()  # oldest → newest
    base_sha = _git(upstream_repo, "rev-parse", "master")

    # Invocation N captures rev-N (1-indexed).
    for inv_idx, source_sha in enumerate(source_revs, start=1):
        # Each invocation opens its OWN writer — mirrors how separate
        # cli.py runs would do it.
        # min_free_bytes=0 to bypass the production 5GB floor — /tmp on
        # CI can have much less. The floor itself is exercised by
        # test_open_returns_none_when_disk_below_floor.
        w = RecordingWriter.open(record_root, PR_URL, min_free_bytes=0)
        assert w is not None, "writer should open with relaxed floor"
        w.write_pr_meta(
            title="Buy-3-get-1-free promotion",
            description="Customer request CR-555",
            author="alice",
            source_branch="feat",
            target_branch="master",
        )
        w.start_invocation(
            triggered_by={"kind": "comment"},
            message="/review",
            comment_id=1000 + inv_idx,
            agent_name="reviewer",
        )
        assert w.invocation_index == inv_idx

        # Snapshot with one external comment per invocation, simulating
        # human discussion accumulating between rounds.
        comments = [
            CommentSnapshot(
                stable_id=stable_id_for_external_comment(2000 + i),
                bb_id=2000 + i,
                author="bob",
                is_bot=False,
                body=f"comment at rev-{inv_idx}",
                parent_stable_id=None,
                anchor={"file": "OrderService.java",
                         "line": 1, "side": "new",
                         "rev_sha": source_sha},
                created_at="2026-05-10T13:00:00+0000",
                resolved=False,
            )
            for i in range(inv_idx)  # 1, 2, 3 comments respectively
        ]
        rev_id = f"rev-{inv_idx:02d}"
        w.write_snapshot(PRSnapshot(
            base_sha=base_sha,
            source_sha=source_sha,
            source_branch="feat",
            target_branch="master",
            pr_status="open",
            rev_id=rev_id,
            captured_at=f"2026-05-10T14:0{inv_idx}:00+00:00",
            comments=comments,
        ))

        # Jira raw capture
        w.capture_jira_ticket("PROJ-1234", {"key": "PROJ-1234",
                                              "fields": {"summary": "test"}})
        w.capture_jira_dev_info("PROJ-1234", {"detail": [{}]})
        w.capture_jira_search("project = PROJ AND status = 'In Review'",
                              {"issues": [], "total": 0})

        # Agent output
        w.write_output(
            findings=[{"title": "null check missing", "severity": "MAJOR"}],
            posted_comments=[
                {"bb_id": 5000 + inv_idx, "body": "found something"},
            ],
            status_changes=[],
            exit_status="ok",
        )

        # Bundle update
        w.update_bundle(
            str(upstream_repo),
            base_sha=base_sha,
            source_sha=source_sha,
            rev_id=rev_id,
            scope="range",
        )

    # ── Assertions ───────────────────────────────────────────────────────
    pr_dir = record_root / "bitbucket.example.com" / "PROJ" / "orderflow" / "PR-1234"
    assert pr_dir.is_dir()

    # pr.json — single, idempotent
    pr_meta = json.loads((pr_dir / "pr.json").read_text(encoding="utf-8"))
    assert pr_meta["pr_id"] == 1234
    assert pr_meta["title"] == "Buy-3-get-1-free promotion"
    assert pr_meta["source_branch"] == "feat"
    assert pr_meta["target_branch"] == "master"

    # invocations/*/
    inv_dirs = sorted(d for d in (pr_dir / "invocations").iterdir() if d.is_dir())
    assert len(inv_dirs) == 3, f"expected 3 invocation dirs, got {inv_dirs}"

    for idx, inv in enumerate(inv_dirs, start=1):
        assert (inv / "triggered_by.json").exists()
        assert (inv / "snapshot.json").exists()
        assert (inv / "output.json").exists()
        snap = json.loads((inv / "snapshot.json").read_text(encoding="utf-8"))
        assert snap["source_sha"] == source_revs[idx - 1]
        assert snap["rev_id"] == f"rev-{idx:02d}"
        assert len(snap["comments"]) == idx
        out = json.loads((inv / "output.json").read_text(encoding="utf-8"))
        assert out["exit_status"] == "ok"
        # Posted comments get auto-stamped stable_ids.
        assert out["posted_comments"][0]["stable_id"] == stable_id_for_agent_comment(idx, 1)
        # Jira capture
        assert (inv / "jira" / "PROJ-1234.json").exists()
        assert (inv / "jira" / "dev_info" / "PROJ-1234.json").exists()
        searches = list((inv / "jira" / "search").iterdir())
        assert len(searches) == 1

    # manifest.json — tracks all 3 revs
    manifest = json.loads((pr_dir / "manifest.json").read_text(encoding="utf-8"))
    rev_ids = [r["rev_id"] for r in manifest["bundle_revs"]]
    assert rev_ids == ["rev-01", "rev-02", "rev-03"]

    # bundle integrity — restore and verify all refs exist
    bundle = pr_dir / "repo.bundle"
    assert bundle.exists()
    refs_txt = (pr_dir / "refs.txt").read_text(encoding="utf-8")
    assert "refs/diffgraph/PR-1234/base" in refs_txt
    assert "refs/diffgraph/PR-1234/source" in refs_txt
    for rev in ("rev-01", "rev-02", "rev-03"):
        assert f"refs/diffgraph/PR-1234/{rev}" in refs_txt

    with tempfile.TemporaryDirectory() as restored:
        subprocess.run(
            ["git", "clone", "--mirror", "--quiet",
             str(bundle), restored + "/repo.git"],
            check=True, capture_output=True,
        )
        out = subprocess.run(
            ["git", "-C", restored + "/repo.git", "for-each-ref",
             "--format=%(objectname) %(refname)", "refs/diffgraph/"],
            check=True, capture_output=True, text=True,
        ).stdout
        # Same refs as captured in refs.txt
        assert sorted(out.strip().splitlines()) == sorted(refs_txt.strip().splitlines())

        # Each rev-N ref must resolve to the corresponding source_sha.
        for idx, source_sha in enumerate(source_revs, start=1):
            ref = f"refs/diffgraph/PR-1234/rev-{idx:02d}"
            resolved = subprocess.run(
                ["git", "-C", restored + "/repo.git", "rev-parse", ref],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            assert resolved == source_sha, (
                f"rev-{idx:02d} should resolve to {source_sha} but got {resolved}"
            )

        # Author identity preserved on the commits in the bundle.
        # (Confirms "truthful repo" rule 3 from TODO §19.7).
        commit_meta = subprocess.run(
            ["git", "-C", restored + "/repo.git", "log", "--format=%an <%ae>",
             f"refs/diffgraph/PR-1234/rev-03"],
            check=True, capture_output=True, text=True,
        ).stdout.strip().splitlines()
        assert all(line == "alice <alice@example.com>" for line in commit_meta)


def test_disabled_writer_on_oserror(record_root: Path, monkeypatch) -> None:
    """A persistent write failure flips the writer to disabled and
    subsequent calls become silent no-ops. Critical so a degraded
    disk doesn't keep raising for every captured event."""
    w = RecordingWriter.open(record_root, PR_URL, min_free_bytes=0)
    assert w is not None

    # Simulate failure by tampering with the private flag — easier than
    # filling the disk inside a test. Verifies the no-op contract.
    w._disabled = True
    # All these MUST silently return without raising.
    w.write_pr_meta(title="x", description="", author="", source_branch="",
                    target_branch="")
    w.start_invocation(triggered_by={"kind": "test"})
    w.write_snapshot(PRSnapshot(
        base_sha="a", source_sha="b", source_branch="", target_branch="",
        pr_status="open", rev_id="rev-01", captured_at="now",
    ))
    w.capture_jira_ticket("X", {})
    w.write_output(findings=[])
    # No bundle attempt either — passing a non-existent path would raise
    # without the guard.
    w.update_bundle("/does/not/exist", base_sha="x", source_sha="y",
                     rev_id="rev-01")
