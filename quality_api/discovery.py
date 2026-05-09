"""Auto-plan discovery — git branches → QA plans.

Implements §5c "discover → plan → run" loop, materialised for git
branches as generations and commit SHAs as mutations.

Crash-resilience invariants:
- All state in SQLite (qa_auto_plan_configs + qa_planned_commits).
- discover() is idempotent — two consecutive calls with the same
  repo state produce zero new plans the second time. Backed by
  qa_planned_commits (PRIMARY KEY on config_id × branch × sha ×
  plan_kind).
- A crash mid-discover() at worst leaves a planned-commits row
  inserted but the corresponding plan row missing — easy to
  reconcile on next call (we re-insert with INSERT OR IGNORE).
- Two run policies per config:
    * unit  — fired on every new commit. Cheap sentinel.
    * full  — fired periodically (full_period_seconds) per branch
      so we still see drift on branches that only get rare commits.

Generation = git branch (e.g. master, feature/foo).
Mutation   = commit SHA on that branch.
Plan kind  = 'unit' (sentinel matrix) or 'full' (whole matrix).
"""
from __future__ import annotations

import fnmatch
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .queue import PlanSpec, PlanStore, TaskQueue


# ── Config row helpers ──────────────────────────────────────────────────────

@dataclass
class AutoPlanConfig:
    id: int
    name: str
    repo_path: str
    branch_pattern: str
    providers: list[str]
    unit_scenarios: list[str]
    full_scenarios: list[str]
    full_period_seconds: int
    attempts_min: int
    enabled: bool
    created_at: str
    last_discover_at: Optional[str]


def _row_to_config(row: sqlite3.Row | None) -> Optional[AutoPlanConfig]:
    if row is None:
        return None
    def _arr(s):
        try:
            return json.loads(s) if s else []
        except Exception:
            return []
    return AutoPlanConfig(
        id=int(row["id"]),
        name=row["name"] or "",
        repo_path=row["repo_path"],
        branch_pattern=row["branch_pattern"],
        providers=_arr(row["providers"]),
        unit_scenarios=_arr(row["unit_scenarios"]),
        full_scenarios=_arr(row["full_scenarios"]),
        full_period_seconds=int(row["full_period_seconds"] or 86400),
        attempts_min=int(row["attempts_min"] or 1),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_discover_at=row["last_discover_at"],
    )


def config_to_dict(c: AutoPlanConfig) -> dict:
    return {
        "id": c.id, "name": c.name,
        "repo_path": c.repo_path, "branch_pattern": c.branch_pattern,
        "providers": c.providers,
        "unit_scenarios": c.unit_scenarios,
        "full_scenarios": c.full_scenarios,
        "full_period_seconds": c.full_period_seconds,
        "attempts_min": c.attempts_min,
        "enabled": c.enabled,
        "created_at": c.created_at,
        "last_discover_at": c.last_discover_at,
    }


# ── Git helpers ─────────────────────────────────────────────────────────────

def git_list_branches(repo_path: str) -> list[tuple[str, str]]:
    """Return [(branch, sha)] for all local branches in `repo_path`.

    Uses `git for-each-ref` for stability — works whether refs are
    packed or unpacked, doesn't need libgit2.
    """
    p = Path(repo_path).expanduser()
    if not (p / ".git").exists() and not p.name == ".git":
        return []
    try:
        out = subprocess.check_output(
            ["git", "-C", str(p), "for-each-ref",
             "--format=%(refname:short) %(objectname)", "refs/heads/"],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except Exception:
        return []
    pairs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        branch, sha = line.split(" ", 1)
        pairs.append((branch.strip(), sha.strip()))
    return pairs


def match_branch(branch: str, pattern: str) -> bool:
    """Single pattern. CSV is exploded by the caller."""
    return fnmatch.fnmatch(branch, pattern.strip())


def match_any(branch: str, patterns_csv: str) -> bool:
    if not patterns_csv:
        return False
    for p in patterns_csv.split(","):
        p = p.strip()
        if p and match_branch(branch, p):
            return True
    return False


# ── Store ───────────────────────────────────────────────────────────────────

class AutoPlanStore:
    """CRUD over qa_auto_plan_configs + the crash-resilient discovery
    loop that consults qa_planned_commits to avoid double-runs."""

    def __init__(self, queue: TaskQueue):
        self.queue = queue
        self.plans = PlanStore(queue)

    def add_config(self, *,
                   name: str = "",
                   repo_path: str,
                   branch_pattern: str,
                   providers: list[str],
                   unit_scenarios: list[str],
                   full_scenarios: list[str] | None = None,
                   full_period_seconds: int = 86400,
                   attempts_min: int = 1,
                   enabled: bool = True) -> int:
        if not providers:
            raise ValueError("providers required")
        if not unit_scenarios:
            raise ValueError("unit_scenarios required (at least sentinel set)")
        repo_path = str(Path(repo_path).expanduser().resolve())
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_auto_plan_configs
                   (name, repo_path, branch_pattern, providers,
                    unit_scenarios, full_scenarios, full_period_seconds,
                    attempts_min, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name or "", repo_path, branch_pattern,
                 json.dumps(providers, ensure_ascii=False),
                 json.dumps(unit_scenarios, ensure_ascii=False),
                 json.dumps(full_scenarios or [], ensure_ascii=False),
                 int(full_period_seconds), int(attempts_min),
                 1 if enabled else 0,
                 datetime.now().isoformat()),
            )
            c.commit()
            return int(cur.lastrowid)

    def list_configs(self, *, only_enabled: bool = False) -> list[AutoPlanConfig]:
        clause = "WHERE enabled=1" if only_enabled else ""
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                f"SELECT * FROM qa_auto_plan_configs {clause} ORDER BY id"
            ).fetchall()
        return [_row_to_config(r) for r in rows]

    def get_config(self, config_id: int) -> Optional[AutoPlanConfig]:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                "SELECT * FROM qa_auto_plan_configs WHERE id=?", (config_id,)
            ).fetchone()
        return _row_to_config(row)

    def set_enabled(self, config_id: int, enabled: bool) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                "UPDATE qa_auto_plan_configs SET enabled=? WHERE id=?",
                (1 if enabled else 0, config_id),
            )
            c.commit()
            return cur.rowcount > 0

    def delete_config(self, config_id: int) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                "DELETE FROM qa_auto_plan_configs WHERE id=?", (config_id,)
            )
            c.commit()
            return cur.rowcount > 0

    # ── Discovery sweep ─────────────────────────────────────────────────

    def discover(self, *, config_id: Optional[int] = None) -> list[dict]:
        """Walk one or all enabled configs; create plans for unseen
        (branch, sha, kind) triples. Returns list of created plan
        descriptors: [{config_id, branch, sha, plan_kind, plan_id, task_count}].
        Existing rows are skipped (idempotent).
        """
        configs = (
            [self.get_config(config_id)] if config_id is not None
            else self.list_configs(only_enabled=True)
        )
        configs = [c for c in configs if c is not None]
        created = []
        for cfg in configs:
            for plan in self._discover_one(cfg):
                created.append(plan)
            with self.queue._lock, self.queue._conn() as c:
                c.execute(
                    "UPDATE qa_auto_plan_configs SET last_discover_at=? WHERE id=?",
                    (datetime.now().isoformat(), cfg.id),
                )
                c.commit()
        return created

    def _discover_one(self, cfg: AutoPlanConfig) -> list[dict]:
        out = []
        branches = git_list_branches(cfg.repo_path)
        # Filter against the config's branch pattern (CSV of globs).
        branches = [(b, sha) for (b, sha) in branches
                    if match_any(b, cfg.branch_pattern)]
        # ─── Unit kind: every new commit ───────────────────────────────
        for branch, sha in branches:
            if self._already_planned(cfg.id, branch, sha, "unit"):
                continue
            plan_id, task_ids = self._create_plan(cfg, branch, sha,
                                                  scenarios=cfg.unit_scenarios,
                                                  kind="unit")
            self._record_planned(cfg.id, branch, sha, "unit", plan_id)
            out.append({
                "config_id": cfg.id, "branch": branch, "sha": sha,
                "plan_kind": "unit", "plan_id": plan_id,
                "task_count": len(task_ids),
            })

        # ─── Full kind: periodic per (branch, sha) ─────────────────────
        if cfg.full_scenarios:
            cutoff = (datetime.now() - timedelta(seconds=cfg.full_period_seconds)).isoformat()
            for branch, sha in branches:
                if self._already_planned(cfg.id, branch, sha, "full"):
                    continue
                # Only fire full if no recent full run on THIS branch
                # (cheap protection against running full every minute).
                if self._has_recent_full(cfg.id, branch, cutoff):
                    continue
                plan_id, task_ids = self._create_plan(cfg, branch, sha,
                                                      scenarios=cfg.full_scenarios,
                                                      kind="full")
                self._record_planned(cfg.id, branch, sha, "full", plan_id)
                out.append({
                    "config_id": cfg.id, "branch": branch, "sha": sha,
                    "plan_kind": "full", "plan_id": plan_id,
                    "task_count": len(task_ids),
                })
        return out

    def _create_plan(self, cfg: AutoPlanConfig, branch: str, sha: str,
                     scenarios: list[str], kind: str) -> tuple[int, list[int]]:
        # Per-task payload carries the auto-plan provenance so a
        # downstream worker can build a `--prompts file://<repo>@<sha>`
        # URL or check out the right ref before invoking the bench.
        plan_name = f"auto:{cfg.name or cfg.id}:{branch}@{sha[:7]}:{kind}"
        spec = PlanSpec(
            name=plan_name,
            created_by=f"auto-plan/config-{cfg.id}/{kind}",
            branches=[branch],
            providers=cfg.providers,
            scenarios=scenarios,
            attempts_min=cfg.attempts_min,
            priority=10 if kind == "unit" else 50,  # unit first
            notes=f"repo={cfg.repo_path} kind={kind}",
        )
        plan_id, task_ids = self.plans.create(spec)
        # Tag every task with mutation_hash = sha so /api/search/runs
        # can join them.
        with self.queue._lock, self.queue._conn() as c:
            c.executemany(
                "UPDATE qa_tasks SET mutation_hash=? WHERE id=?",
                [(sha, tid) for tid in task_ids],
            )
            c.commit()
        return plan_id, task_ids

    # ── Idempotency ledger ──────────────────────────────────────────────

    def _already_planned(self, config_id: int, branch: str,
                         sha: str, kind: str) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                """SELECT 1 FROM qa_planned_commits
                   WHERE config_id=? AND branch=? AND sha=? AND plan_kind=?""",
                (config_id, branch, sha, kind),
            ).fetchone()
        return row is not None

    def _has_recent_full(self, config_id: int, branch: str, since_iso: str) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                """SELECT 1 FROM qa_planned_commits
                   WHERE config_id=? AND branch=? AND plan_kind='full'
                     AND planned_at >= ?""",
                (config_id, branch, since_iso),
            ).fetchone()
        return row is not None

    def _record_planned(self, config_id: int, branch: str,
                        sha: str, kind: str, plan_id: int) -> None:
        with self.queue._lock, self.queue._conn() as c:
            # INSERT OR IGNORE — concurrent discoveries don't double-insert.
            c.execute(
                """INSERT OR IGNORE INTO qa_planned_commits
                   (config_id, branch, sha, plan_kind, plan_id, planned_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (config_id, branch, sha, kind, plan_id,
                 datetime.now().isoformat()),
            )
            c.commit()
