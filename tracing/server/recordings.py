"""QA-server side of the recording feature (TODO §19 UI).

Lists recordings under a configured root, exposes per-recording
detail, and enqueues replay tasks via the QA queue.

The recording root is resolved at module load from the env var
`DIFFGRAPH_RECORDINGS_DIR`. Empty / unset = listing returns
empty + replay returns 404 (feature off). Set the env on the
QA-server process to point at the directory the webhook captures
into.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def root_path() -> Optional[Path]:
    """Configured recordings root, resolved live each call so a
    test fixture or runtime config edit takes effect without
    needing a restart."""
    raw = os.environ.get("DIFFGRAPH_RECORDINGS_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.is_dir() else None


# ── Path ↔ id encoding ──────────────────────────────────────────────────

def encode_id(rel_path: str) -> str:
    """URL-safe encoding of a relative recording path."""
    return base64.urlsafe_b64encode(rel_path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_id(rec_id: str) -> str:
    pad = "=" * (-len(rec_id) % 4)
    return base64.urlsafe_b64decode(rec_id + pad).decode("utf-8")


def safe_recording_dir(rec_id: str) -> Optional[Path]:
    """Decode + validate path stays under root_path()."""
    root = root_path()
    if root is None:
        return None
    try:
        rel = decode_id(rec_id)
    except Exception:
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None  # path-traversal attempt
    if not candidate.is_dir():
        return None
    if not (candidate / "pr.json").is_file():
        return None
    return candidate


# ── Listing ──────────────────────────────────────────────────────────────


@dataclass
class RecordingSummary:
    rec_id: str
    rel_path: str
    server: str
    project: str
    repo: str
    pr_id: int
    title: str
    author: str
    n_invocations: int
    has_bundle: bool
    total_bytes: int
    first_recorded_at: str
    last_recorded_at: str


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def list_recordings() -> list[RecordingSummary]:
    """Walk the recordings root and return one summary per `PR-<id>/`
    directory found. Cheap stat-based walk — no JSON parse beyond
    pr.json + invocation count."""
    root = root_path()
    if root is None:
        return []
    out: list[RecordingSummary] = []
    # Layout: <host>/<project>/<repo>/PR-<id>/pr.json
    for pr_json in root.rglob("pr.json"):
        pr_dir = pr_json.parent
        try:
            meta = json.loads(pr_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("recordings: %s invalid: %s", pr_json, exc)
            continue
        inv_root = pr_dir / "invocations"
        n_inv = len([p for p in inv_root.iterdir() if p.is_dir()]) \
                 if inv_root.is_dir() else 0
        rel = pr_dir.relative_to(root).as_posix()
        out.append(RecordingSummary(
            rec_id=encode_id(rel),
            rel_path=rel,
            server=str(meta.get("server", "")),
            project=str(meta.get("project", "")),
            repo=str(meta.get("repo", "")),
            pr_id=int(meta.get("pr_id", 0) or 0),
            title=str(meta.get("title", "")),
            author=str(meta.get("author", "")),
            n_invocations=n_inv,
            has_bundle=(pr_dir / "repo.bundle").is_file(),
            total_bytes=_dir_size(pr_dir),
            first_recorded_at=str(meta.get("first_recorded_at", "")),
            last_recorded_at=str(meta.get("last_recorded_at", "")),
        ))
    out.sort(key=lambda s: s.last_recorded_at, reverse=True)
    return out


# ── Detail ───────────────────────────────────────────────────────────────


def load_detail(rec_id: str) -> Optional[dict]:
    """Full detail: pr meta + manifest + per-invocation summary
    (no bundle content). Returns None when the rec_id doesn't
    resolve under the configured root."""
    pr_dir = safe_recording_dir(rec_id)
    if pr_dir is None:
        return None
    pr_meta = json.loads((pr_dir / "pr.json").read_text(encoding="utf-8"))
    manifest_p = pr_dir / "manifest.json"
    manifest = json.loads(manifest_p.read_text(encoding="utf-8")) \
        if manifest_p.is_file() else {}
    # Outcomes (TODO §19.9) — optional; serves as ground truth for
    # lifecycle metrics when present.
    outcomes_summary: Optional[dict] = None
    outcomes_p = pr_dir / "outcomes.yaml"
    if outcomes_p.is_file():
        try:
            import yaml
            raw = yaml.safe_load(outcomes_p.read_text(encoding="utf-8")) or {}
            labels = raw.get("labels") or {}
            verdicts: dict[str, int] = {}
            for lbl in labels.values():
                if isinstance(lbl, dict):
                    v = str(lbl.get("verdict", "undecided"))
                    verdicts[v] = verdicts.get(v, 0) + 1
            outcomes_summary = {
                "exists":       True,
                "n_labels":     len(labels),
                "n_missed":     len(raw.get("missed_findings") or []),
                "verdicts":     verdicts,
                "labelled_by":  raw.get("labelled_by", ""),
                "labelled_at":  raw.get("labelled_at", ""),
            }
        except Exception:
            outcomes_summary = {"exists": True, "error": "malformed"}
    invocations: list[dict] = []
    inv_root = pr_dir / "invocations"
    if inv_root.is_dir():
        for inv_dir in sorted(p for p in inv_root.iterdir() if p.is_dir()):
            trig_p = inv_dir / "triggered_by.json"
            snap_p = inv_dir / "snapshot.json"
            out_p = inv_dir / "output.json"
            try:
                trig = json.loads(trig_p.read_text(encoding="utf-8")) \
                    if trig_p.is_file() else {}
                snap = json.loads(snap_p.read_text(encoding="utf-8")) \
                    if snap_p.is_file() else {}
                out = json.loads(out_p.read_text(encoding="utf-8")) \
                    if out_p.is_file() else None
            except (OSError, json.JSONDecodeError):
                continue
            try:
                idx = int(inv_dir.name.split("-", 1)[0])
            except (ValueError, IndexError):
                continue
            invocations.append({
                "index":       idx,
                "dir_name":    inv_dir.name,
                "triggered_by": trig,
                "rev_id":      snap.get("rev_id", ""),
                "base_sha":    snap.get("base_sha", ""),
                "source_sha":  snap.get("source_sha", ""),
                "pr_status":   snap.get("pr_status", ""),
                "n_comments":  len(snap.get("comments", []) or []),
                "captured_at": snap.get("captured_at", ""),
                "output_summary": _summarize_output(out),
                "jira_count": _jira_files(inv_dir),
            })
    invocations.sort(key=lambda i: i["index"])
    return {
        "rec_id":      rec_id,
        "pr_dir":      str(pr_dir),
        "pr_meta":     pr_meta,
        "manifest":    manifest,
        "invocations": invocations,
        "has_bundle":  (pr_dir / "repo.bundle").is_file(),
        "outcomes":    outcomes_summary,
    }


def _summarize_output(out: Optional[dict]) -> dict:
    if not out:
        return {"exit_status": "unknown", "n_findings": 0, "n_posted": 0}
    return {
        "exit_status":   out.get("exit_status", "unknown"),
        "n_findings":    len(out.get("findings", []) or []),
        "n_posted":      len(out.get("posted_comments", []) or []),
        "n_status":      len(out.get("status_changes", []) or []),
        "error":         out.get("error") or "",
        "finished_at":   out.get("finished_at", ""),
    }


def _jira_files(inv_dir: Path) -> dict:
    jira = inv_dir / "jira"
    if not jira.is_dir():
        return {"tickets": 0, "dev_info": 0, "searches": 0}
    tickets = len([p for p in jira.glob("*.json")])
    di = jira / "dev_info"
    dev_info = len(list(di.glob("*.json"))) if di.is_dir() else 0
    s = jira / "search"
    searches = len(list(s.glob("*.json"))) if s.is_dir() else 0
    return {"tickets": tickets, "dev_info": dev_info, "searches": searches}


# ── Replay enqueue ───────────────────────────────────────────────────────


def build_replay_bench_cmd(
    pr_dir: Path, *, mode: str = "single",
    invocation: int | str = "first", provider: str = "deepseek",
) -> str:
    """Build the shell command a worker runs to perform the replay.

    mode='single'   → `bench replay-single <dir> --invocation <N>` (one
                       agent run against one captured state).
    mode='lifecycle'→ `bench replay <dir>` (walks the full timeline,
                       runs the agent at every agent_invocation event
                       with accumulating state; see TODO §19 Phase 3).
    """
    repo = os.environ.get("DIFFGRAPH_REPO") or str(Path(__file__).resolve().parents[2])
    base = (
        f"cd {repo} && source .env && unset ALL_PROXY all_proxy "
        f"&& .venv/bin/python -m benchmarks.cli "
    )
    if mode == "lifecycle":
        return base + f"replay {pr_dir} --provider {provider}"
    return base + (
        f"replay-single {pr_dir} --invocation {invocation} "
        f"--provider {provider}"
    )


def enqueue_replay(_qa_queue, rec_id: str, *,
                   mode: str = "single",
                   invocation: int | str = "first",
                   queue: str = "deepseek",
                   priority: int = 100) -> Optional[int]:
    """Enqueue a qa_task that runs replay-single or replay (lifecycle)
    for this recording. Returns the new task id, or None when the
    rec_id doesn't resolve (404 from the route layer)."""
    pr_dir = safe_recording_dir(rec_id)
    if pr_dir is None:
        return None
    from quality_api.queue import TaskSpec  # late import to avoid cycles

    bench_cmd = build_replay_bench_cmd(
        pr_dir, mode=mode, invocation=invocation, provider=queue,
    )
    payload = {
        "kind":         "replay",
        "recording":    str(pr_dir),
        "mode":         mode,
        "invocation":   invocation,
        "bench_cmd":    bench_cmd,
        "plan_name":    (
            f"replay-lifecycle:{pr_dir.name}" if mode == "lifecycle"
            else f"replay:{pr_dir.name}:inv-{invocation}"
        ),
    }
    spec = TaskSpec(
        queue=queue,
        priority=priority,
        payload=payload,
        resources=[f"recording://{pr_dir.relative_to(root_path()).as_posix()}"],
    )
    task_id = _qa_queue.enqueue(spec)
    return task_id
