"""Replay tier — load a recording, reconstruct PR state, run agent.

See TODO §19. This module is the **reader** counterpart of
`diffgraph.recording` (the writer). It does not know about bench
scenarios as YAML — it speaks the recording-layout shape directly.

Three layers:

  1. `RecordingReader.load(dir)` — parses pr.json + manifest.json +
     all invocations.
  2. `RecordingReader.materialize_repo(inv_index, tmp_dir)` — clones
     from repo.bundle into a fresh repo and checks out the rev-N
     SHA. Returns the cloned path. Cross-mount-safe.
  3. `RecordingReader.build_fake_payload(inv_index)` — produces the
     payload dict FakeBitbucket consumes (same shape unit-tier
     fixtures emit).

Used by:
  - `bench replay-single` — pick one invocation, run agent, score.
  - Lifecycle replay driver (TODO §19 Phase 3, separate module).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Data classes mirroring the on-disk layout ────────────────────────────


@dataclass
class Invocation:
    """One captured agent run within a recording."""
    index: int                       # 1-based; matches "001-..." dir prefix
    dir: Path
    triggered_by: dict
    snapshot: dict                   # raw snapshot.json content
    output: Optional[dict]           # None if agent crashed before write_output
    jira_dir: Path                   # may not exist if agent never called jira


@dataclass
class Recording:
    """One PR worth of captured invocations."""
    pr_dir: Path
    pr_meta: dict                    # raw pr.json content
    manifest: dict                   # raw manifest.json content
    invocations: list[Invocation] = field(default_factory=list)

    @property
    def pr_id(self) -> int:
        return int(self.pr_meta.get("pr_id", 0))

    @property
    def has_bundle(self) -> bool:
        return (self.pr_dir / "repo.bundle").is_file()


# ── Loader ───────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Dir-name format: `NNN-<UTC-stamp>-<rand>`. Loader sorts by index, NOT
# by lex order — three-digit prefix already sorts right, but a stale
# legacy two-digit dir would scramble that.
_INV_DIR_RE = re.compile(r"^(\d+)-")


class RecordingReader:
    """Pure-read parser for a recording directory."""

    @staticmethod
    def load(pr_dir: str | Path) -> Recording:
        pr_dir = Path(pr_dir).expanduser().resolve()
        if not pr_dir.is_dir():
            raise FileNotFoundError(f"recording dir not found: {pr_dir}")
        pr_json = pr_dir / "pr.json"
        if not pr_json.is_file():
            raise FileNotFoundError(
                f"recording is missing pr.json at {pr_json} — "
                "is this really a recording dir?"
            )
        manifest_p = pr_dir / "manifest.json"
        manifest = _read_json(manifest_p) if manifest_p.is_file() else {}

        invocations: list[Invocation] = []
        inv_root = pr_dir / "invocations"
        if inv_root.is_dir():
            for inv_dir in sorted(p for p in inv_root.iterdir() if p.is_dir()):
                m = _INV_DIR_RE.match(inv_dir.name)
                if not m:
                    log.warning("recording: skipping malformed inv dir %s",
                                inv_dir.name)
                    continue
                idx = int(m.group(1))
                tb_p = inv_dir / "triggered_by.json"
                sn_p = inv_dir / "snapshot.json"
                out_p = inv_dir / "output.json"
                if not sn_p.is_file():
                    log.warning("recording: %s has no snapshot.json — skipping",
                                inv_dir.name)
                    continue
                invocations.append(Invocation(
                    index=idx,
                    dir=inv_dir,
                    triggered_by=_read_json(tb_p) if tb_p.is_file() else {},
                    snapshot=_read_json(sn_p),
                    output=_read_json(out_p) if out_p.is_file() else None,
                    jira_dir=inv_dir / "jira",
                ))
        invocations.sort(key=lambda i: i.index)
        return Recording(
            pr_dir=pr_dir,
            pr_meta=_read_json(pr_json),
            manifest=manifest,
            invocations=invocations,
        )

    @staticmethod
    def find_invocation(rec: Recording, selector: int | str) -> Invocation:
        """Resolve a numeric index or one of {'first', 'last'}."""
        if not rec.invocations:
            raise ValueError(f"recording {rec.pr_dir} has no invocations")
        if isinstance(selector, str):
            s = selector.strip().lower()
            if s == "first":
                return rec.invocations[0]
            if s == "last":
                return rec.invocations[-1]
            try:
                selector = int(s)
            except ValueError:
                raise ValueError(f"unknown invocation selector: {selector!r}")
        for inv in rec.invocations:
            if inv.index == int(selector):
                return inv
        raise ValueError(
            f"invocation {selector} not in recording (have: "
            f"{[i.index for i in rec.invocations]})"
        )


# ── Repo materialisation ─────────────────────────────────────────────────


def materialize_repo(rec: Recording, inv: Invocation,
                     target_dir: str | Path) -> Path:
    """Restore the bundle into target_dir/repo and check out the
    rev-N source SHA from this invocation.

    Returns the path of the working tree (target_dir/repo). The
    bundle is cloned as a normal (non-mirror) repo so the bench's
    existing flow — which expects a working tree — works unchanged.
    """
    target = Path(target_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    repo = target / "repo"

    bundle = rec.pr_dir / "repo.bundle"
    if not bundle.is_file():
        raise FileNotFoundError(
            f"bundle missing for {rec.pr_dir} — recording is incomplete; "
            "capture may have failed mid-way"
        )

    # Step 1: clone the bundle as a regular (non-mirror) repo so we
    # get a working tree. Bundle is a single packfile, fast.
    subprocess.run(
        ["git", "clone", "--quiet", str(bundle), str(repo)],
        check=True, capture_output=True,
    )

    # Step 2: fetch the diffgraph/* refs (clone doesn't pick non-heads
    # refs by default — it took refs/heads/* via the bundle's HEAD).
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "--quiet",
         "origin", "refs/diffgraph/*:refs/diffgraph/*"],
        check=False, capture_output=True,
    )

    # Step 3: check out the rev-N SHA as a detached HEAD. Also create
    # the source_branch as a local ref pointing here so tools that
    # read `--branch <name>` still resolve.
    source_sha = inv.snapshot.get("source_sha") or ""
    source_branch = inv.snapshot.get("source_branch") or "feat"
    target_branch = inv.snapshot.get("target_branch") or "master"
    base_sha = inv.snapshot.get("base_sha") or ""

    if not source_sha:
        raise ValueError(
            f"invocation {inv.index} has no source_sha — recording corrupt"
        )

    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet",
         "-B", source_branch, source_sha],
        check=True, capture_output=True,
    )
    # Stamp the base branch too — bench's fake_view + diff tooling
    # expect both refs to exist by name.
    if base_sha:
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-f",
             target_branch, base_sha],
            check=False, capture_output=True,
        )
    # Disconnect the clone from the bundle — bundles aren't usable
    # as long-lived remotes for re-fetch.
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"],
                   check=False, capture_output=True)
    return repo


# ── FakeBitbucket payload synthesis ──────────────────────────────────────


def build_fake_payload(rec: Recording, inv: Invocation,
                       repo_path: Path,
                       *, self_user: str = "diffgraph-bot") -> dict:
    """Build the payload FakeBitbucket consumes for this invocation.

    Mirrors the shape `benchmarks/runner/run_unit.py:_build_fake_pr_payload`
    produces from a UnitFixture — same keys, same semantics — so the
    existing FakeBenchPRView + fake-bitbucket plumbing accepts it
    without changes.
    """
    snap = inv.snapshot
    pr_meta = rec.pr_meta

    # Map captured snapshot comments → fake-PR comments list shape.
    # Captured comments carry stable_id; FakeBitbucket keys by bb_id.
    # We pass both through so the replay scoring can correlate.
    comments = []
    for c in snap.get("comments", []) or []:
        comments.append({
            "id":         c.get("bb_id"),
            "stable_id":  c.get("stable_id"),
            "text":       c.get("body", ""),
            "author":     c.get("author", ""),
            "parent_id":  None if c.get("parent_stable_id") is None else
                         _bb_id_from_stable(c["parent_stable_id"],
                                            snap.get("comments", [])),
            "anchor":     c.get("anchor"),
            "created_at": c.get("created_at", ""),
            "resolved":   bool(c.get("resolved", False)),
        })

    pr_url = pr_meta.get("pr_url") or (
        f"fake://recording/{pr_meta.get('project', 'PROJ')}/"
        f"repos/{pr_meta.get('repo', 'repo')}/"
        f"pull-requests/{pr_meta.get('pr_id', 1)}"
    )

    return {
        "pr_url":      pr_url,
        "repo_path":   str(repo_path),
        "base_sha":    snap.get("base_sha", ""),
        "source_sha":  snap.get("source_sha", ""),
        "metadata":    {
            "pr_url":           pr_url,
            "title":            pr_meta.get("title", ""),
            "description":      pr_meta.get("description", ""),
            "author":           pr_meta.get("author", ""),
            "from_branch":      snap.get("source_branch", ""),
            "to_branch":        snap.get("target_branch", ""),
            "state":            snap.get("pr_status", "open"),
        },
        "comments":    comments,
        "self_user":   self_user,
        # Hand off the recording-side context so scoring can correlate.
        "_recording_context": {
            "pr_dir":            str(rec.pr_dir),
            "invocation_index":  inv.index,
            "rev_id":            snap.get("rev_id", ""),
            "captured_at":       snap.get("captured_at", ""),
        },
    }


def _bb_id_from_stable(parent_stable_id: str, all_comments: list[dict]) -> Optional[int]:
    """Resolve a stable_id back to the bb_id from the snapshot, so
    FakeBitbucket's parent_id chain works as the agent expects.
    Returns None if not found — orphan-skip handled elsewhere."""
    for c in all_comments:
        if c.get("stable_id") == parent_stable_id:
            return c.get("bb_id")
    return None


# ── Jira fixture synthesis from captured raw responses ───────────────────


def synthesize_unit_fixture_yaml(
    rec: Recording, inv: Invocation, *,
    workspace: Path, repo_path: Path,
) -> Path:
    """Write a unit-tier fixture yaml on disk that drives this
    invocation through the existing `bench run-unit` machinery.

    This is the bridge between recording-shape (declarative state
    snapshots) and unit-fixture-shape (declarative scenario yaml the
    bench already loads). Lets replay-single piggy-back on every
    bench feature unit-tier has: judge, posted-action sink, OTel
    trace, structural asserts.
    """
    import yaml
    snap = inv.snapshot
    pr_meta = rec.pr_meta

    # Map snapshot comments → unit-fixture comment shape.
    comments_out: list[dict] = []
    by_stable: dict[str, int] = {}
    for c in snap.get("comments", []) or []:
        bb_id = c.get("bb_id")
        stable_id = c.get("stable_id")
        if isinstance(bb_id, int) and stable_id:
            by_stable[stable_id] = bb_id

    for c in snap.get("comments", []) or []:
        bb_id = c.get("bb_id")
        if bb_id is None:
            continue
        anchor = c.get("anchor") or {}
        parent_stable = c.get("parent_stable_id")
        parent_bb = by_stable.get(parent_stable, 0) if parent_stable else 0
        author = c.get("author") or "anonymous"
        comments_out.append({
            "id":        bb_id,
            "parent_id": parent_bb,
            "file":      anchor.get("file") or "",
            "line":      anchor.get("line") or 0,
            "text":      c.get("body") or "",
            "author":    {"name": author, "slug": author},
            "timestamp": c.get("created_at") or "",
            "resolved":  bool(c.get("resolved", False)),
        })

    trig = inv.triggered_by or {}
    trigger_block: dict = {}
    if trig.get("comment_id") is not None:
        trigger_block["comment_id"] = trig["comment_id"]
    if trig.get("message"):
        trigger_block["text"] = trig["message"]

    # Agent: prefer the recorded agent_name; default to "reviewer".
    agent = trig.get("agent_name") or "reviewer"

    fixture_id = f"replay-PR{rec.pr_id}-inv{inv.index:03d}"
    fixture_yaml = {
        "id":     fixture_id,
        "agent":  agent,
        "tags":   ["tier:replay", f"recording:{rec.pr_dir.name}",
                   f"inv:{inv.index:03d}"],
        "repo": {
            "source":        str(repo_path),
            "base_branch":   snap.get("target_branch") or "master",
            "source_branch": snap.get("source_branch") or "feat",
        },
        "pr_state": {
            "metadata": {
                "title":       pr_meta.get("title", ""),
                "description": pr_meta.get("description", ""),
                "pr_url":      pr_meta.get("pr_url", ""),
                "bot_user":    "diffgraph-bot",
            },
            "comments":   comments_out,
            "self_user":  "diffgraph-bot",
        },
        "trigger": trigger_block,
    }

    # Jira fixture if any was captured.
    jira_target = workspace / "jira-fixture.yaml"
    if build_jira_fixture(inv, jira_target):
        fixture_yaml["jira_fixture"] = str(jira_target)

    target = workspace / "scenario.yaml"
    target.write_text(
        yaml.safe_dump(fixture_yaml, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


def build_jira_fixture(inv: Invocation, target_path: Path) -> Optional[Path]:
    """Convert the captured jira/ raw responses into a single yaml
    fixture the JiraProvider can read in fake mode.

    The JiraProvider's extended-fixture format is:
        {issue: {...}, dev_info: {...}, searches: {<jql>: <response>}}

    Multiple tickets in the same recording invocation become MULTIPLE
    fixtures (one per ticket) keyed by `key`. The bench's existing
    handle/namespace/key plumbing serves them as long as the
    DIFFGRAPH_JIRA_FIXTURE env points at the FIRST one and others
    sit alongside — JiraProvider falls back to the right file by ref
    parsing.

    For now we just produce the FIRST ticket fixture (single-ticket
    replays). Returns its path, or None if no Jira was captured.
    """
    if not inv.jira_dir.is_dir():
        return None
    ticket_files = sorted(
        p for p in inv.jira_dir.glob("*.json") if p.is_file()
    )
    if not ticket_files:
        return None

    first_key = ticket_files[0].stem
    issue_raw = _read_json(ticket_files[0])
    dev_info = None
    di_path = inv.jira_dir / "dev_info" / f"{first_key}.json"
    if di_path.is_file():
        dev_info = _read_json(di_path)
    searches = {}
    s_dir = inv.jira_dir / "search"
    if s_dir.is_dir():
        for s_path in s_dir.glob("*.json"):
            try:
                blob = _read_json(s_path)
                searches[blob.get("jql", "")] = blob.get("response", {})
            except (OSError, json.JSONDecodeError):
                continue

    fixture = {
        "issue":     issue_raw,
        "dev_info":  dev_info or {},
        "searches":  searches,
    }
    import yaml
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump(fixture, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target_path
