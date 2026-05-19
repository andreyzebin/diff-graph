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


# ── Timeline builder (Phase 3 foundation) ────────────────────────────────


@dataclass
class TimelineEvent:
    """One event in the chronological replay timeline.

    Synthesized by comparing consecutive invocation snapshots — the
    capture writer only persists STATE snapshots, the replay-side
    derives the EVENTS between them. This decouples capture (one
    snapshot per invocation, cheap) from replay (an unbounded event
    stream, parameterizable).
    """
    at_ts: str                         # ISO timestamp from the source snapshot/output
    kind: str                          # see TODO §19.6 table
    # Kind-specific payload fields. Free-form dict — driver dispatches on `kind`.
    data: dict = field(default_factory=dict)


def build_timeline(rec: Recording) -> list[TimelineEvent]:
    """Derive a chronological event list from the recording.

    Algorithm:
      - Initial state: emit `pr_opened` from invocation 1's snapshot.
      - Between invocations N and N+1:
          • If source_sha changed → `commit_pushed` (or `force_pushed`
            when the new SHA is not an ancestor descendant of the old —
            the bundle's ancestry is the source of truth, but we
            conservatively treat any SHA change as commit_pushed for
            now; force-push detection is a Phase 3.5 refinement).
          • New comments (in snap[N+1] but not snap[N]) → `comment_added`.
          • Comments that flipped resolved=True/False → `comment_resolved`
            / `comment_reopened`.
          • pr_status change → `status_changed`.
      - At each invocation: emit `agent_invocation` event.
      - At the end (if pr_status is 'merged' in the last snapshot or
        the recording's pr.json has merged_at): emit `pr_merged`.

    All events carry `at_ts`. Order = chronological by `at_ts`, with
    ties broken by emission order (deterministic given the snapshots).
    """
    events: list[TimelineEvent] = []
    if not rec.invocations:
        return events

    # Initial PR open — capture only writes pr.json once per PR, so
    # pr_opened isn't an event we observe; we synthesise it from the
    # first invocation's snapshot.
    first = rec.invocations[0]
    first_snap = first.snapshot
    events.append(TimelineEvent(
        at_ts=str(rec.pr_meta.get("created_at") or first_snap.get("captured_at", "")),
        kind="pr_opened",
        data={
            "base_sha":     first_snap.get("base_sha", ""),
            "source_sha":   first_snap.get("source_sha", ""),
            "source_branch": first_snap.get("source_branch", ""),
            "target_branch": first_snap.get("target_branch", ""),
            "title":        rec.pr_meta.get("title", ""),
            "description":  rec.pr_meta.get("description", ""),
            "author":       rec.pr_meta.get("author", ""),
        },
    ))

    prev_snap: Optional[dict] = None
    prev_rev: Optional[str] = None
    for inv in rec.invocations:
        snap = inv.snapshot
        rev_id = snap.get("rev_id", "")

        # Delta vs previous snapshot
        if prev_snap is not None:
            # 1. Source SHA advanced.
            if snap.get("source_sha") != prev_snap.get("source_sha"):
                events.append(TimelineEvent(
                    at_ts=snap.get("captured_at", ""),
                    kind="commit_pushed",
                    data={
                        "rev_id":           rev_id,
                        "new_source_sha":   snap.get("source_sha", ""),
                        "prior_source_sha": prev_snap.get("source_sha", ""),
                        "prior_rev_id":     prev_rev or "",
                    },
                ))
            # 2. Comment deltas — new + resolved/reopened.
            prev_by_stable = {
                (c.get("stable_id") or ""): c
                for c in (prev_snap.get("comments") or [])
            }
            curr_by_stable = {
                (c.get("stable_id") or ""): c
                for c in (snap.get("comments") or [])
            }
            for sid, c in curr_by_stable.items():
                if sid not in prev_by_stable:
                    events.append(TimelineEvent(
                        at_ts=str(c.get("created_at") or snap.get("captured_at", "")),
                        kind="comment_added",
                        data={
                            "stable_id":        sid,
                            "bb_id":            c.get("bb_id"),
                            "author":           c.get("author", ""),
                            "is_bot":           bool(c.get("is_bot")),
                            "body":             c.get("body", ""),
                            "parent_stable_id": c.get("parent_stable_id"),
                            "anchor":           c.get("anchor"),
                            "resolved":         bool(c.get("resolved", False)),
                        },
                    ))
                else:
                    prev_c = prev_by_stable[sid]
                    if bool(prev_c.get("resolved")) != bool(c.get("resolved")):
                        events.append(TimelineEvent(
                            at_ts=snap.get("captured_at", ""),
                            kind=("comment_resolved" if c.get("resolved")
                                  else "comment_reopened"),
                            data={"stable_id": sid, "bb_id": c.get("bb_id")},
                        ))
            # 3. PR status change.
            if snap.get("pr_status") != prev_snap.get("pr_status"):
                events.append(TimelineEvent(
                    at_ts=snap.get("captured_at", ""),
                    kind="status_changed",
                    data={"to": snap.get("pr_status", "")},
                ))
        else:
            # First invocation — seed all comments from the snapshot as
            # comment_added events so the replay has a populated thread
            # graph from t0.
            for c in (snap.get("comments") or []):
                events.append(TimelineEvent(
                    at_ts=str(c.get("created_at") or snap.get("captured_at", "")),
                    kind="comment_added",
                    data={
                        "stable_id":        c.get("stable_id"),
                        "bb_id":            c.get("bb_id"),
                        "author":           c.get("author", ""),
                        "is_bot":           bool(c.get("is_bot")),
                        "body":             c.get("body", ""),
                        "parent_stable_id": c.get("parent_stable_id"),
                        "anchor":           c.get("anchor"),
                        "resolved":         bool(c.get("resolved", False)),
                    },
                ))

        # The invocation itself.
        trig = inv.triggered_by or {}
        events.append(TimelineEvent(
            at_ts=snap.get("captured_at", ""),
            kind="agent_invocation",
            data={
                "index":         inv.index,
                "rev_id":        rev_id,
                "triggered_by":  trig.get("kind", "unknown"),
                "comment_id":    trig.get("comment_id"),
                "message":       trig.get("message", ""),
                "agent_name":    trig.get("agent_name", "reviewer"),
                # Reference for scoring: where the recorded agent's
                # baseline output lives.
                "_recorded_output_path": str(inv.dir / "output.json"),
            },
        ))

        prev_snap = snap
        prev_rev = rev_id

    # Final merge / close event if visible in last snapshot.
    last_status = (rec.invocations[-1].snapshot or {}).get("pr_status", "")
    if last_status in ("merged", "declined"):
        events.append(TimelineEvent(
            at_ts=rec.invocations[-1].snapshot.get("captured_at", ""),
            kind=("pr_merged" if last_status == "merged" else "pr_declined"),
            data={"merge_sha": rec.invocations[-1].snapshot.get("source_sha", "")},
        ))
    return events


# ── Identity model state (Phase 3) ────────────────────────────────────────


@dataclass
class ReplayState:
    """Mutable state the lifecycle driver evolves as it walks the
    timeline. The captured snapshot is read-only; this is its
    runtime mirror — comments accumulate, source_sha advances,
    agent outputs land here for subsequent events to see.
    """
    base_sha: str = ""
    source_sha: str = ""
    source_branch: str = ""
    target_branch: str = ""
    pr_status: str = "open"
    title: str = ""
    description: str = ""
    author: str = ""

    # Runtime comments — full Bitbucket-fake shape, mutable.
    comments: list[dict] = field(default_factory=list)

    # Identity map: stable_id (captured) → bb_id (runtime).
    # Populated as the world events apply. Agent comments live in
    # `comments` but DO NOT enter this map (they have no stable_id
    # because the recording doesn't own them).
    stable_to_bb_id: dict[str, int] = field(default_factory=dict)

    # Skipped-as-orphan log: stable_ids whose parent didn't exist in
    # the runtime state when the event fired. Surfaced as a
    # divergence_signal in the aggregate.
    orphan_skips: list[dict] = field(default_factory=list)

    # Allocator for fresh runtime IDs (for agent comments + any human
    # comment that lacked a bb_id in the recording).
    next_runtime_bb_id: int = 1_000_000


@dataclass
class InvocationReplay:
    """Result of replaying one agent_invocation event."""
    index: int
    rev_id: str
    exit_code: int
    judge_score: Optional[float]
    judge_verdict: Optional[str]
    posted_comments: list[dict]
    recorded_baseline_path: str
    stdout_tail: str
    stderr_tail: str
    duration_seconds: float
    error: Optional[str] = None


@dataclass
class LifecycleReplayResult:
    """Aggregate output of a full lifecycle replay."""
    recording_dir: str
    pr_id: int
    n_events: int
    n_invocations_replayed: int
    invocations: list[InvocationReplay]
    orphan_skip_count: int
    orphan_skips: list[dict]
    avg_judge_score: Optional[float]
    final_pr_status: str
    workspace: str

    # Populated when outcomes.yaml is present in the recording and the
    # driver was told to score against it (or scored after the fact via
    # benchmarks.runner.outcomes.score_lifecycle). None when no
    # outcomes were available — replay still produces per-invocation
    # judge scores.
    metrics: Optional[Any] = None

    def to_dict(self) -> dict:
        d = {
            "recording_dir":         self.recording_dir,
            "pr_id":                 self.pr_id,
            "n_events":              self.n_events,
            "n_invocations_replayed": self.n_invocations_replayed,
            "invocations": [
                {
                    "index":          i.index,
                    "rev_id":         i.rev_id,
                    "exit_code":      i.exit_code,
                    "judge_score":    i.judge_score,
                    "judge_verdict":  i.judge_verdict,
                    "n_posted":       len(i.posted_comments),
                    "duration_s":     i.duration_seconds,
                    "error":          i.error,
                } for i in self.invocations
            ],
            "orphan_skip_count":     self.orphan_skip_count,
            "orphan_skips":          self.orphan_skips,
            "avg_judge_score":       self.avg_judge_score,
            "final_pr_status":       self.final_pr_status,
            "workspace":             self.workspace,
        }
        if self.metrics is not None:
            d["metrics"] = (self.metrics.to_dict()
                            if hasattr(self.metrics, "to_dict")
                            else dict(self.metrics))
        return d


# ── Lifecycle driver ─────────────────────────────────────────────────────


class LifecycleReplayDriver:
    """Walks the recording's timeline, manages runtime state, spawns
    the agent at every agent_invocation event.

    Construction is cheap (no I/O). `.run()` does all the heavy work.
    """

    def __init__(self, rec: Recording, *, workspace: Path,
                 provider: Optional[str] = None,
                 timeout: int = 300,
                 judge_cfg: Optional[dict] = None):
        self.rec = rec
        self.workspace = workspace
        self.provider = provider
        self.timeout = timeout
        self.judge_cfg = judge_cfg
        self.state = ReplayState()
        self.events = build_timeline(rec)
        self._results: list[InvocationReplay] = []

    # ── Public ────────────────────────────────────────────────────────

    def run(self) -> LifecycleReplayResult:
        """Drive the full timeline. Returns an aggregate result.

        Best-effort per invocation: an agent that crashes or times
        out is recorded as a failed InvocationReplay and the timeline
        proceeds — subsequent events still apply, subsequent
        invocations still fire. (Mirrors how a real PR would: one
        flaky reviewer comment doesn't undo the next push.)
        """
        for ev in self.events:
            if ev.kind == "agent_invocation":
                self._run_invocation(ev)
            else:
                self._apply_world_event(ev)

        scores = [r.judge_score for r in self._results
                  if r.judge_score is not None]
        avg = sum(scores) / len(scores) if scores else None
        result = LifecycleReplayResult(
            recording_dir=str(self.rec.pr_dir),
            pr_id=self.rec.pr_id,
            n_events=len(self.events),
            n_invocations_replayed=len(self._results),
            invocations=self._results,
            orphan_skip_count=len(self.state.orphan_skips),
            orphan_skips=self.state.orphan_skips,
            avg_judge_score=avg,
            final_pr_status=self.state.pr_status,
            workspace=str(self.workspace),
        )
        # If outcomes.yaml is present alongside the recording, score
        # against it for business metrics (miss_rate, noise_rate, …).
        # Best-effort: a malformed outcomes.yaml is reported in logs
        # but doesn't break the replay run.
        try:
            from .outcomes import load_outcomes, score_lifecycle
            outcomes = load_outcomes(self.rec.pr_dir / "outcomes.yaml")
            if outcomes is not None:
                recorded = _build_recorded_findings_index(self.rec)
                result.metrics = score_lifecycle(
                    replay_result=result,
                    outcomes=outcomes,
                    recorded_findings_by_invocation=recorded,
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("replay: outcomes scoring failed: %s", exc)
        return result

    # ── World-event handlers ──────────────────────────────────────────

    def _apply_world_event(self, ev: TimelineEvent) -> None:
        h = getattr(self, f"_on_{ev.kind}", None)
        if h is None:
            log.debug("replay: ignoring unknown event kind %s", ev.kind)
            return
        h(ev)

    def _on_pr_opened(self, ev: TimelineEvent) -> None:
        d = ev.data
        self.state.base_sha = d.get("base_sha", "")
        self.state.source_sha = d.get("source_sha", "")
        self.state.source_branch = d.get("source_branch", "")
        self.state.target_branch = d.get("target_branch", "")
        self.state.title = d.get("title", "")
        self.state.description = d.get("description", "")
        self.state.author = d.get("author", "")

    def _on_commit_pushed(self, ev: TimelineEvent) -> None:
        self.state.source_sha = ev.data.get("new_source_sha",
                                              self.state.source_sha)

    def _on_force_pushed(self, ev: TimelineEvent) -> None:
        # Same effect on state; the divergence signal is on the kind.
        self._on_commit_pushed(ev)

    def _on_comment_added(self, ev: TimelineEvent) -> None:
        d = ev.data
        sid = d.get("stable_id") or ""
        parent_sid = d.get("parent_stable_id")
        # Orphan-skip rule (TODO §19.4).
        parent_bb_id: Optional[int] = None
        if parent_sid:
            parent_bb_id = self.state.stable_to_bb_id.get(parent_sid)
            if parent_bb_id is None:
                self.state.orphan_skips.append({
                    "stable_id":         sid,
                    "missing_parent":    parent_sid,
                    "at_ts":             ev.at_ts,
                    "author":            d.get("author"),
                })
                return
        # Assign a runtime bb_id. Prefer the captured one when present.
        bb_id = d.get("bb_id")
        if not isinstance(bb_id, int) or bb_id <= 0:
            bb_id = self._fresh_bb_id()
        if sid:
            self.state.stable_to_bb_id[sid] = bb_id
        self.state.comments.append({
            "id":         bb_id,
            "stable_id":  sid,
            "parent_id":  parent_bb_id or 0,
            "text":       d.get("body", ""),
            "author":     d.get("author", ""),
            "anchor":     d.get("anchor"),
            "is_bot":     bool(d.get("is_bot")),
            "resolved":   bool(d.get("resolved", False)),
            "created_at": ev.at_ts,
        })

    def _on_comment_resolved(self, ev: TimelineEvent) -> None:
        sid = ev.data.get("stable_id") or ""
        bb_id = self.state.stable_to_bb_id.get(sid)
        if bb_id is None:
            return  # orphan
        for c in self.state.comments:
            if c.get("id") == bb_id:
                c["resolved"] = True
                break

    def _on_comment_reopened(self, ev: TimelineEvent) -> None:
        sid = ev.data.get("stable_id") or ""
        bb_id = self.state.stable_to_bb_id.get(sid)
        if bb_id is None:
            return
        for c in self.state.comments:
            if c.get("id") == bb_id:
                c["resolved"] = False
                break

    def _on_status_changed(self, ev: TimelineEvent) -> None:
        self.state.pr_status = ev.data.get("to", self.state.pr_status)

    def _on_pr_merged(self, ev: TimelineEvent) -> None:
        self.state.pr_status = "merged"

    def _on_pr_declined(self, ev: TimelineEvent) -> None:
        self.state.pr_status = "declined"

    # ── Invocation runner ─────────────────────────────────────────────

    def _run_invocation(self, ev: TimelineEvent) -> None:
        """Spawn cli.py for one agent_invocation event using the
        current runtime state. Output's posted_comments are
        integrated back into self.state so subsequent events see
        them (lifecycle accumulation)."""
        import time
        from .run_unit import run_unit_fixture

        idx = int(ev.data.get("index", 0))
        rec_inv = next((i for i in self.rec.invocations if i.index == idx),
                       None)
        if rec_inv is None:
            log.warning("replay: agent_invocation index=%d has no recording", idx)
            return

        inv_workspace = self.workspace / f"inv-{idx:03d}"
        inv_workspace.mkdir(parents=True, exist_ok=True)
        try:
            repo = materialize_repo(self.rec, rec_inv, inv_workspace)
        except Exception as exc:
            log.warning("replay: materialize_repo failed for inv %d: %s", idx, exc)
            self._results.append(InvocationReplay(
                index=idx, rev_id=rec_inv.snapshot.get("rev_id", ""),
                exit_code=2, judge_score=None, judge_verdict=None,
                posted_comments=[],
                recorded_baseline_path=str(rec_inv.dir / "output.json"),
                stdout_tail="", stderr_tail=str(exc),
                duration_seconds=0.0, error=f"materialize: {exc}",
            ))
            return

        fixture_yaml = self._synthesize_invocation_fixture(
            rec_inv, repo, inv_workspace, current_state=self.state,
        )

        t0 = time.time()
        try:
            result = run_unit_fixture(
                fixture_yaml,
                provider=self.provider,
                timeout=self.timeout,
                keep_tmp_on_success=False,
                attempt_dir=str(inv_workspace / "attempt"),
                judge_cfg=self.judge_cfg or None,
            )
        except Exception as exc:
            log.warning("replay: run_unit_fixture raised for inv %d: %s", idx, exc)
            self._results.append(InvocationReplay(
                index=idx, rev_id=rec_inv.snapshot.get("rev_id", ""),
                exit_code=3, judge_score=None, judge_verdict=None,
                posted_comments=[],
                recorded_baseline_path=str(rec_inv.dir / "output.json"),
                stdout_tail="", stderr_tail=str(exc),
                duration_seconds=time.time() - t0,
                error=f"runner: {exc}",
            ))
            return

        # Integrate posted comments into state so the NEXT events see them
        # as if the agent really had posted to Bitbucket.
        posted = list(result.posted or [])
        for rec in posted:
            if rec.get("kind") != "pr_post_comment":
                continue
            bb_id = rec.get("new_id") or self._fresh_bb_id()
            self.state.comments.append({
                "id":         bb_id,
                "stable_id":  "",   # agent comments have no captured stable_id
                "parent_id":  rec.get("parent_id") or 0,
                "text":       rec.get("text", ""),
                "author":     "diffgraph-bot",
                "is_bot":     True,
                "anchor": {
                    "file": rec.get("file", ""),
                    "line": rec.get("line", 0),
                    "side": rec.get("side", "new"),
                },
                "resolved":   False,
                "created_at": "",
            })
        # And any status change.
        for rec in posted:
            if rec.get("kind") == "set_status":
                # `status` is the Bitbucket participant status —
                # APPROVED / NEEDS_WORK / UNAPPROVED. Map to our
                # state's pr_status approximation.
                self.state.pr_status = (rec.get("status") or
                                          self.state.pr_status).lower()

        self._results.append(InvocationReplay(
            index=idx,
            rev_id=rec_inv.snapshot.get("rev_id", ""),
            exit_code=result.exit_code,
            judge_score=result.judge_score,
            judge_verdict=result.judge_verdict,
            posted_comments=posted,
            recorded_baseline_path=str(rec_inv.dir / "output.json"),
            stdout_tail=result.stdout_tail or "",
            stderr_tail=result.stderr_tail or "",
            duration_seconds=time.time() - t0,
            error=None if result.exit_code == 0 else "non-zero exit",
        ))

    # ── Helpers ───────────────────────────────────────────────────────

    def _fresh_bb_id(self) -> int:
        bb = self.state.next_runtime_bb_id
        self.state.next_runtime_bb_id += 1
        return bb

    def _synthesize_invocation_fixture(
        self, rec_inv: Invocation, repo: Path,
        inv_workspace: Path, *, current_state: ReplayState,
    ) -> Path:
        """Build a unit-tier fixture YAML using the CURRENT runtime
        state (not the recorded snapshot) — that's what makes this
        lifecycle replay rather than per-invocation replay. Each
        agent run sees:
          - The accumulated comments (humans verbatim + bot's own
            past outputs from earlier invocations)
          - The current source/base SHAs
          - The captured trigger info for THIS invocation point
        """
        import yaml
        trig = rec_inv.triggered_by or {}

        # Map runtime comments → unit-fixture pr_state.comments shape.
        comments_out: list[dict] = []
        for c in current_state.comments:
            anchor = c.get("anchor") or {}
            author_name = c.get("author") or "anonymous"
            comments_out.append({
                "id":        c.get("id"),
                "parent_id": c.get("parent_id") or 0,
                "file":      anchor.get("file") or "",
                "line":      anchor.get("line") or 0,
                "text":      c.get("text") or "",
                "author":    {"name": author_name, "slug": author_name},
                "timestamp": c.get("created_at") or "",
                "resolved":  bool(c.get("resolved", False)),
            })

        trigger_block: dict = {}
        if trig.get("comment_id") is not None:
            trigger_block["comment_id"] = trig["comment_id"]
        if trig.get("message"):
            trigger_block["text"] = trig["message"]

        agent = trig.get("agent_name") or "reviewer"
        fixture_id = (
            f"replay-PR{self.rec.pr_id}-inv{rec_inv.index:03d}-"
            f"lifecycle"
        )

        fixture_yaml = {
            "id":     fixture_id,
            "agent":  agent,
            "tags":   ["tier:replay", "mode:lifecycle",
                       f"recording:{self.rec.pr_dir.name}",
                       f"inv:{rec_inv.index:03d}"],
            "repo": {
                "source":        str(repo),
                "base_branch":   current_state.target_branch or "master",
                "source_branch": current_state.source_branch or "feat",
            },
            "pr_state": {
                "metadata": {
                    "title":       current_state.title,
                    "description": current_state.description,
                    "pr_url":      self.rec.pr_meta.get("pr_url", ""),
                    "bot_user":    "diffgraph-bot",
                    "state":       current_state.pr_status,
                },
                "comments":   comments_out,
                "self_user":  "diffgraph-bot",
            },
            "trigger": trigger_block,
        }
        # Jira fixture, captured at this invocation.
        jira_target = inv_workspace / "jira-fixture.yaml"
        if build_jira_fixture(rec_inv, jira_target):
            fixture_yaml["jira_fixture"] = str(jira_target)

        target = inv_workspace / "scenario.yaml"
        target.write_text(
            yaml.safe_dump(fixture_yaml, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return target


def _build_recorded_findings_index(rec: Recording) -> dict[int, list[dict]]:
    """Map invocation_index → list of agent comments recorded at that
    invocation. Used by outcomes.score_lifecycle to look up the
    recorded peer of a replay-posted comment."""
    out: dict[int, list[dict]] = {}
    for inv in rec.invocations:
        bucket: list[dict] = []
        out_data = inv.output or {}
        for c in (out_data.get("posted_comments") or []):
            bucket.append({
                "stable_id": c.get("stable_id", ""),
                "bb_id":     c.get("bb_id"),
                "file":      c.get("file", "") or
                             (c.get("anchor", {}) or {}).get("file", ""),
                "line":      c.get("line", 0) or
                             (c.get("anchor", {}) or {}).get("line", 0),
                "body":      c.get("body", "") or c.get("text", ""),
            })
        out[inv.index] = bucket
    return out


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
