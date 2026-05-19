"""
Recording (capture) layer — writes per-PR replay fixtures to disk.

See TODO §19 for the design. This module owns the on-disk file
layout and atomic appends; it does NOT decide when to capture
(that's cli.py + webhook integration) or how to replay (that's
benchmarks/runner/replay.py).

File layout (one PR per recording):

    recordings/<server>/<project>/<repo>/PR-<id>/
    ├── pr.json                         # static metadata; written once
    ├── manifest.json                   # bundle scope + bundle_revs[]
    ├── invocations/
    │   ├── 001-<ts>/
    │   │   ├── snapshot.json           # PR state at run start
    │   │   ├── triggered_by.json       # what kicked the agent off
    │   │   ├── output.json             # what the agent produced
    │   │   ├── jira/<KEY>.json         # full raw responses per ticket
    │   │   └── jira/search/<idx>.json  # JQL responses, indexed
    │   └── 002-<ts>/...
    ├── repo.bundle                     # git bundle, incrementally re-built
    ├── refs.txt                        # for-each-ref snapshot of bundle
    └── timeline.json                   # derived view (built lazily from
                                        # invocations + bundle)

Concurrency:
    Each invocation gets its own directory by timestamp+pid+rand,
    so concurrent cli.py runs on the same PR don't collide. pr.json
    and manifest.json use atomic write-and-rename. timeline.json is
    NOT touched during capture — it's a derived view.

The capture path is best-effort: if writing the recording fails
for any reason (disk full, permissions, race), the agent run
proceeds unaffected. Errors are logged at WARNING, never raised.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import random
import shutil
import string
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Free-space floor for capture. Refuses to write below this so a
# runaway recording dir doesn't wedge the host the way /tmp/unit-*
# did during the May-2026 cleanup. Configurable via env.
_DEFAULT_MIN_FREE_BYTES = 5 * 1024 ** 3  # 5 GB


def _utc_now_isoformat() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _utc_stamp() -> str:
    """Compact timestamp for directory names: 2026-05-19T14-22-19Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _rand_suffix(n: int = 4) -> str:
    """Short tail to disambiguate concurrent invocations on the same PR."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON via tmp + rename. Survives mid-write crash without
    leaving the destination file half-populated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def _read_json_or_default(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _has_free_space(target_dir: Path, min_bytes: int) -> bool:
    try:
        st = shutil.disk_usage(str(target_dir))
        return st.free >= min_bytes
    except OSError:
        # Path doesn't exist yet — check the closest ancestor.
        p = target_dir
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            st = shutil.disk_usage(str(p))
            return st.free >= min_bytes
        except OSError:
            return True  # don't block on indeterminate state


# ── PR-URL → record-root path mapping ────────────────────────────────────

def pr_dir_for(record_root: Path, server: str, project: str, repo: str,
               pr_id: int) -> Path:
    """Where on disk a recording for this PR lives.

    `server` is the URL we got from parse_pr_url (`https://host/path`).
    Path component is `<host>` only — paths like `bitbucket-ci` in the
    URL go into a sub-segment so different deployments on the same
    host don't collide.
    """
    from urllib.parse import urlparse
    parsed = urlparse(server)
    host = parsed.netloc or "unknown-host"
    suffix = (parsed.path or "").strip("/").replace("/", "_")
    host_segment = f"{host}__{suffix}" if suffix else host
    return record_root / host_segment / project / repo / f"PR-{pr_id}"


# ── Stable IDs for comments ──────────────────────────────────────────────
#
# For human (and other non-bot) comments captured from Bitbucket, the
# upstream integer id is unique and stable across replays — we just
# prefix `c-` to namespace them away from agent-issued ids. For agent
# comments we use `a-<inv>-<seq>` where <inv> is the invocation index
# this run, <seq> is the order within that invocation. This survives
# even if Bitbucket reassigns numeric ids on replay.

def stable_id_for_external_comment(bb_id: int | str) -> str:
    return f"c-{bb_id}"


def stable_id_for_agent_comment(invocation_index: int, seq_within: int) -> str:
    return f"a-{invocation_index:03d}-{seq_within:02d}"


# ── Snapshot building ────────────────────────────────────────────────────

@dataclass
class CommentSnapshot:
    """One comment in PR state at invocation time."""
    stable_id: str
    bb_id: int | str
    author: str
    is_bot: bool
    body: str
    parent_stable_id: Optional[str]
    anchor: Optional[dict]  # {file, line, side, rev_sha} or None for top-level
    created_at: str
    resolved: bool

    def to_dict(self) -> dict:
        d = {
            "stable_id":    self.stable_id,
            "bb_id":        self.bb_id,
            "author":       self.author,
            "is_bot":       self.is_bot,
            "body":         self.body,
            "parent_stable_id": self.parent_stable_id,
            "anchor":       self.anchor,
            "created_at":   self.created_at,
            "resolved":     self.resolved,
        }
        return {k: v for k, v in d.items() if v is not None or k in (
            "anchor", "parent_stable_id",
        )}


@dataclass
class PRSnapshot:
    """The complete PR state at one moment in time.

    Captured at the START of every cli.py invocation. Multiple
    snapshots stacked under invocations/ let the replay-loader
    reconstruct the timeline by diffing consecutive snapshots.
    """
    base_sha: str
    source_sha: str
    source_branch: str
    target_branch: str
    pr_status: str        # "open" | "merged" | "declined" | unknown
    rev_id: str            # "rev-NN" assigned to source_sha for bundle ref
    captured_at: str
    comments: list[CommentSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base_sha":        self.base_sha,
            "source_sha":      self.source_sha,
            "source_branch":   self.source_branch,
            "target_branch":   self.target_branch,
            "pr_status":       self.pr_status,
            "rev_id":          self.rev_id,
            "captured_at":     self.captured_at,
            "comments":        [c.to_dict() for c in self.comments],
        }


# ── Writer ───────────────────────────────────────────────────────────────

class RecordingWriter:
    """One writer per cli.py invocation.

    Lifecycle:
        w = RecordingWriter.open(record_root, pr_url, ...)
        w.write_pr_meta(...)                # idempotent
        w.start_invocation(triggered_by=...) # picks the invocation dir
        w.write_snapshot(snapshot)
        w.capture_jira_response(key, raw)   # 0..N times during agent run
        w.capture_jira_search(jql, raw)     # 0..N times
        ...agent runs...
        w.write_output(findings=..., posted_comments=..., status_changes=...)
        w.update_bundle(repo_path)          # may be deferred
        w.finalize()                        # flushes manifest

    All write paths are best-effort: any IOError / OSError is caught
    and logged at WARNING. The agent run is never aborted by a
    recording failure.
    """

    def __init__(self, pr_dir: Path, pr_url: str, server: str,
                 project: str, repo: str, pr_id: int,
                 min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES):
        self.pr_dir = pr_dir
        self.pr_url = pr_url
        self.server = server
        self.project = project
        self.repo = repo
        self.pr_id = pr_id
        self.min_free_bytes = min_free_bytes

        self._invocation_dir: Optional[Path] = None
        self._invocation_index: int = 0
        self._jira_idx_search: int = 0
        self._disabled: bool = False

        self.pr_dir.mkdir(parents=True, exist_ok=True)

    # ── Constructors ─────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        record_root: str | Path,
        pr_url: str,
        *,
        min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
    ) -> Optional["RecordingWriter"]:
        """Resolve record-root from pr_url and return a writer.

        Returns None if the URL can't be parsed (we silently skip
        recording rather than crash) OR if disk space is below the
        configured floor (we log a warning and skip).
        """
        from .bitbucket import parse_pr_url
        try:
            server, project, repo, pr_id = parse_pr_url(pr_url)
        except Exception as exc:
            log.warning("recording: cannot parse pr_url=%r — capture disabled: %s",
                        pr_url, exc)
            return None

        record_root_p = Path(record_root).expanduser().resolve()

        if not _has_free_space(record_root_p, min_free_bytes):
            log.warning("recording: free space below floor=%dB at %s — capture disabled",
                        min_free_bytes, record_root_p)
            return None

        pr_dir = pr_dir_for(record_root_p, server, project, repo, pr_id)
        try:
            pr_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("recording: cannot mkdir %s — capture disabled: %s",
                        pr_dir, exc)
            return None

        return cls(pr_dir, pr_url, server, project, repo, pr_id,
                   min_free_bytes=min_free_bytes)

    # ── pr.json (static metadata, idempotent) ───────────────────────

    def write_pr_meta(self, *, title: str, description: str, author: str,
                      source_branch: str, target_branch: str,
                      created_at: Optional[str] = None) -> None:
        path = self.pr_dir / "pr.json"
        existing = _read_json_or_default(path, {})
        # First write wins for title/description/author/created_at —
        # later runs may capture an evolved title/description, but
        # the AT-CREATION values are what matters for replay context.
        meta = {
            "pr_url":         self.pr_url,
            "server":         self.server,
            "project":        self.project,
            "repo":           self.repo,
            "pr_id":          self.pr_id,
            "title":          existing.get("title", title) or title,
            "description":    existing.get("description", description) or description,
            "author":         existing.get("author", author) or author,
            "source_branch":  source_branch or existing.get("source_branch", ""),
            "target_branch":  target_branch or existing.get("target_branch", ""),
            "created_at":     existing.get("created_at") or created_at or _utc_now_isoformat(),
            "first_recorded_at": existing.get("first_recorded_at", _utc_now_isoformat()),
            "last_recorded_at":  _utc_now_isoformat(),
        }
        try:
            _atomic_write_json(path, meta)
        except OSError as exc:
            self._handle_write_failure("pr.json", exc)

    # ── Invocation lifecycle ─────────────────────────────────────────

    def start_invocation(self, *, triggered_by: dict,
                         message: str = "",
                         comment_id: int = -1,
                         agent_name: str = "",
                         trace_run_id: str = "") -> Path:
        """Allocate the per-invocation directory and write triggered_by.json.

        The directory name encodes the invocation index + timestamp +
        random tail. Index is computed by scanning existing
        invocations/* dirs (atomic-enough since each dir is unique by
        random tail; the "index" is just for human ordering).
        """
        existing = sorted([p.name for p in (self.pr_dir / "invocations").glob("*")
                          if p.is_dir()]) if (self.pr_dir / "invocations").exists() else []
        # next index = max existing index + 1; fall back to len(existing)+1
        next_idx = len(existing) + 1
        for name in existing:
            try:
                # name format: NNN-<stamp>-<rand>
                num = int(name.split("-", 1)[0])
                if num >= next_idx:
                    next_idx = num + 1
            except (ValueError, IndexError):
                pass

        dirname = f"{next_idx:03d}-{_utc_stamp()}-{_rand_suffix()}"
        inv_dir = self.pr_dir / "invocations" / dirname
        try:
            inv_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._handle_write_failure(f"mkdir invocations/{dirname}", exc)
            return inv_dir  # _disabled is now True; all subsequent calls no-op

        self._invocation_dir = inv_dir
        self._invocation_index = next_idx

        try:
            _atomic_write_json(inv_dir / "triggered_by.json", {
                "kind":         triggered_by.get("kind", "unknown"),
                "comment_id":   comment_id if comment_id >= 0 else None,
                "message":      message,
                "agent_name":   agent_name,
                "trace_run_id": trace_run_id,
                "started_at":   _utc_now_isoformat(),
                **{k: v for k, v in triggered_by.items() if k != "kind"},
            })
        except OSError as exc:
            self._handle_write_failure("triggered_by.json", exc)
        return inv_dir

    def write_snapshot(self, snapshot: PRSnapshot) -> None:
        if self._disabled or self._invocation_dir is None:
            return
        try:
            _atomic_write_json(self._invocation_dir / "snapshot.json",
                               snapshot.to_dict())
        except OSError as exc:
            self._handle_write_failure("snapshot.json", exc)

    # ── Jira raw response capture ───────────────────────────────────

    def capture_jira_ticket(self, key: str, raw: dict) -> None:
        if self._disabled or self._invocation_dir is None:
            return
        jira_dir = self._invocation_dir / "jira"
        try:
            _atomic_write_json(jira_dir / f"{key}.json", raw)
        except OSError as exc:
            self._handle_write_failure(f"jira/{key}.json", exc)

    def capture_jira_dev_info(self, key: str, raw: dict) -> None:
        if self._disabled or self._invocation_dir is None:
            return
        jira_dir = self._invocation_dir / "jira" / "dev_info"
        try:
            _atomic_write_json(jira_dir / f"{key}.json", raw)
        except OSError as exc:
            self._handle_write_failure(f"jira/dev_info/{key}.json", exc)

    def capture_jira_search(self, jql: str, raw: dict) -> None:
        if self._disabled or self._invocation_dir is None:
            return
        # JQL is unbounded prose — we hash it for a stable filename and
        # keep the original JQL inside the JSON for human read-back.
        jql_hash = hashlib.sha1(jql.encode("utf-8")).hexdigest()[:12]
        search_dir = self._invocation_dir / "jira" / "search"
        try:
            _atomic_write_json(search_dir / f"{jql_hash}.json", {
                "jql": jql, "response": raw,
            })
        except OSError as exc:
            self._handle_write_failure(f"jira/search/{jql_hash}.json", exc)

    # ── Output capture ──────────────────────────────────────────────

    def write_output(
        self,
        *,
        findings: Optional[list] = None,
        posted_comments: Optional[list[dict]] = None,
        status_changes: Optional[list[dict]] = None,
        exit_status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        """What the agent produced this invocation.

        `findings`         — list of finding dicts (file/line/severity/...)
        `posted_comments`  — comments the agent actually wrote to Bitbucket
                             via pr_post_comment (each: {bb_id, anchor, body,
                             parent_bb_id?})
        `status_changes`   — list of {to: "approved"|"needs_work"|...}
        """
        if self._disabled or self._invocation_dir is None:
            return
        # Stamp agent comments with a-<inv>-<seq> stable IDs so replay
        # tracking can identify them later. Numbering survives even if
        # Bitbucket renumbered between capture and replay.
        comments = posted_comments or []
        stamped: list[dict] = []
        for seq, c in enumerate(comments, start=1):
            c_out = dict(c)
            c_out.setdefault(
                "stable_id",
                stable_id_for_agent_comment(self._invocation_index, seq),
            )
            stamped.append(c_out)

        try:
            _atomic_write_json(self._invocation_dir / "output.json", {
                "findings":         findings or [],
                "posted_comments":  stamped,
                "status_changes":   status_changes or [],
                "exit_status":      exit_status,
                "error":            error,
                "finished_at":      _utc_now_isoformat(),
            })
        except OSError as exc:
            self._handle_write_failure("output.json", exc)

    # ── Bundle creation ─────────────────────────────────────────────

    def update_bundle(
        self,
        repo_path: str,
        *,
        base_sha: str,
        source_sha: str,
        rev_id: str,
        scope: str = "range",
    ) -> None:
        """Re-build repo.bundle to include all rev-N refs so far.

        Strategy:
          1. Clone repo_path into a scratch repo (mirror clone — keeps
             ALL refs, fast on local FS).
          2. Set/update `refs/diffgraph/PR-<id>/<rev_id>` → source_sha
             and a stable `refs/diffgraph/PR-<id>/base` → base_sha.
          3. `git bundle create` with the chosen scope.
          4. Atomic-replace repo.bundle and refs.txt.

        On any failure: leave the previous bundle in place, log
        warning. The recording is still usable — only the latest
        revision is missing.
        """
        if self._disabled:
            return

        scratch = None
        try:
            scratch = Path(tempfile.mkdtemp(prefix="diffgraph-rec-bundle-"))
            mirror = scratch / "repo.git"
            # mirror clone — fast, doesn't depend on remote
            subprocess.run(
                ["git", "clone", "--mirror", "--quiet",
                 repo_path, str(mirror)],
                check=True, capture_output=True,
            )

            # Pull in previously-stamped rev-N refs from the prior
            # bundle (if any) so each invocation accumulates rather
            # than overwrites. Without this, the bundle for rev-N only
            # has rev-N and the earlier revs vanish when this writer's
            # mirror is discarded.
            prior_bundle = self.pr_dir / "repo.bundle"
            if prior_bundle.exists():
                subprocess.run(
                    ["git", "-C", str(mirror), "fetch", "--quiet",
                     str(prior_bundle),
                     "refs/diffgraph/*:refs/diffgraph/*"],
                    check=False, capture_output=True,
                )

            # Stamp our per-revision refs.
            ref_base = f"refs/diffgraph/PR-{self.pr_id}/base"
            ref_rev  = f"refs/diffgraph/PR-{self.pr_id}/{rev_id}"
            ref_src  = f"refs/diffgraph/PR-{self.pr_id}/source"
            for ref, sha in [(ref_base, base_sha),
                             (ref_rev,  source_sha),
                             (ref_src,  source_sha)]:
                if sha:
                    subprocess.run(["git", "-C", str(mirror), "update-ref",
                                    ref, sha], check=False, capture_output=True)

            # Build the bundle.
            new_bundle = self.pr_dir / "repo.bundle.new"
            if scope == "full":
                subprocess.run(["git", "-C", str(mirror), "bundle", "create",
                                str(new_bundle), "--all"],
                               check=True, capture_output=True)
            else:
                # range mode: bundle only the refs/diffgraph/* refs we
                # stamped, with full ancestry walked from each — git
                # bundle includes all reachable objects from the given
                # tips, so `git checkout <rev-N>` works after restore.
                # Enumerate refs explicitly (git bundle ignores
                # --branches=<glob> for non-refs/heads names; passing
                # ref names directly is the documented portable form).
                diffgraph_refs = subprocess.run(
                    ["git", "-C", str(mirror), "for-each-ref",
                     "--format=%(refname)", "refs/diffgraph/"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip().splitlines()
                if not diffgraph_refs:
                    raise RuntimeError("no refs/diffgraph/* refs to bundle "
                                       "(update-ref calls all silently failed?)")
                subprocess.run(["git", "-C", str(mirror), "bundle", "create",
                                str(new_bundle), *diffgraph_refs],
                               check=True, capture_output=True)

            # refs.txt parallel artefact for integrity audit.
            refs_out = subprocess.run(
                ["git", "-C", str(mirror), "for-each-ref",
                 "--format=%(objectname) %(refname)",
                 "refs/diffgraph/"],
                check=True, capture_output=True, text=True,
            ).stdout

            # Atomic swap.
            target_bundle = self.pr_dir / "repo.bundle"
            target_refs   = self.pr_dir / "refs.txt"
            os.replace(new_bundle, target_bundle)
            target_refs.write_text(refs_out, encoding="utf-8")

            # Manifest — track which revs are in the bundle.
            self._update_manifest(rev_id=rev_id, source_sha=source_sha,
                                  base_sha=base_sha, scope=scope)
        except subprocess.CalledProcessError as exc:
            log.warning("recording: git bundle failed: %s\nstderr=%r",
                        exc, (exc.stderr.decode(errors='replace') if exc.stderr else ''))
        except Exception as exc:
            log.warning("recording: bundle update failed: %s", exc)
        finally:
            if scratch and scratch.exists():
                shutil.rmtree(scratch, ignore_errors=True)

    def _update_manifest(self, *, rev_id: str, source_sha: str,
                         base_sha: str, scope: str) -> None:
        path = self.pr_dir / "manifest.json"
        existing = _read_json_or_default(path, {})
        revs = list(existing.get("bundle_revs") or [])
        entry = {"rev_id": rev_id, "source_sha": source_sha}
        # Replace existing entry for same rev_id, else append.
        revs = [r for r in revs if r.get("rev_id") != rev_id]
        revs.append(entry)
        revs.sort(key=lambda r: r.get("rev_id", ""))
        manifest = {
            "pr_url":          self.pr_url,
            "scope":           scope,
            "base_sha":        base_sha or existing.get("base_sha", ""),
            "bundle_revs":     revs,
            "last_updated":    _utc_now_isoformat(),
        }
        try:
            _atomic_write_json(path, manifest)
        except OSError as exc:
            self._handle_write_failure("manifest.json", exc)

    # ── Internal helpers ────────────────────────────────────────────

    def _handle_write_failure(self, what: str, exc: Exception) -> None:
        """Disable the writer on first persistent failure (disk full,
        permissions). Don't keep trying to write — capture is best-
        effort; subsequent calls are silent no-ops."""
        log.warning("recording: %s write failed at %s: %s — disabling capture",
                    what, self.pr_dir, exc)
        self._disabled = True

    # ── Public lookup ───────────────────────────────────────────────

    @property
    def invocation_index(self) -> int:
        """Current invocation index (1-based). Used by callers that
        need to issue stable_id_for_agent_comment() during the run."""
        return self._invocation_index

    @property
    def disabled(self) -> bool:
        return self._disabled


def is_enabled() -> Optional[str]:
    """Return the configured recording root if the feature is on,
    or None. Env-driven so cli.py + webhook both share one switch."""
    return os.environ.get("DIFFGRAPH_RECORD_DIR") or None
