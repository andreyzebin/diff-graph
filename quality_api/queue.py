"""Task queue store over SQLite — lease/heartbeat/finish primitives.

The queue lives in the same DB as traces (~/.diffgraph/traces.db)
so a single `quality-cli` query can join `qa_tasks` to `runs` and
ask things like "what task produced this run / what mutation is
this task on / show me lease-stalled tasks". Single source of truth.

Atomicity:
- Lease wraps the SELECT + UPDATE in BEGIN IMMEDIATE so two
  workers polling at the same time can't both grab the same
  task. SQLite serialises immediate transactions.
- Heartbeat is a single UPDATE.
- Finish is a single UPDATE; idempotent if called twice with the
  same payload (last write wins).
- Reaper does the "stale lease → queued" sweep — pure UPDATE,
  no race because dead workers can't fight back.

State machine:
  queued → leased → running → finished | error | cancelled
                          ↘ (heartbeat timeout) ↗
                            queued (via reaper)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid

log = logging.getLogger(__name__)
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from orchestra.trace_db import DEFAULT_DB_PATH


def _now() -> datetime:
    """UTC-aware datetime — all timestamps stored in DB are UTC ISO
    with `+00:00` offset; client renders to browser locale via
    fmtLocal()."""
    return datetime.now(timezone.utc)


DEFAULT_LEASE_SECONDS = 60
DEFAULT_HEARTBEAT_GRACE_SECONDS = 180  # bumped from 30: brief GC / IO pauses
                                       # in a live worker shouldn't release its
                                       # lease (a queue/supervisor wake-up will
                                       # still re-collect after sleep+resume).
DEFAULT_MAX_RETRIES = 3                # transient errors (non_zero_exit /
                                       # timeout / lease_expired) auto-retry up
                                       # to this many times before marked final.
RETRYABLE_ERROR_CLASSES = frozenset({
    "non_zero_exit", "timeout", "lease_expired", "worker_died",
})


# ── Models ───────────────────────────────────────────────────────────────────

@dataclass
class TaskSpec:
    """What a worker needs to actually run a task. Caller-supplied."""
    scenario_id: str
    provider: str
    attempt_n: int = 1
    lineage: str = ""
    mutation_hash: str = ""
    plan_id: Optional[int] = None
    priority: int = 100              # lower = sooner
    payload: dict = field(default_factory=dict)
    not_before: Optional[str] = None  # ISO datetime; lease() honours this
    kind: str = "agent"              # 'agent' | 'judge' (B-light phantom)
    parent_task_id: Optional[int] = None
    initial_state: str = "queued"    # 'queued' for normal, 'blocked' for judge phantom


@dataclass
class TaskRow:
    """Persisted shape — what /qa/tasks endpoints return.

    `lineage` = a long-lived line of git commits we're testing as a
    hypothesis (typically a git branch like `master` or `feature/foo`);
    `mutation_hash` = a specific commit on that lineage we benched
    against.
    """
    id: int
    state: str
    scenario_id: str
    provider: str
    attempt_n: int
    lineage: str
    mutation_hash: str
    plan_id: Optional[int]
    priority: int
    payload: dict
    lease_owner: Optional[str]
    lease_expires_at: Optional[str]
    enqueued_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    trace_run_id: Optional[str]
    result_json: Optional[dict]
    error_class: Optional[str]
    retry_count: int = 0
    max_retries: int = 3


# ── Store ────────────────────────────────────────────────────────────────────

class TaskQueue:
    """SQLite-backed task queue. Idempotent schema bootstrap on init."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    # ── Connection helpers ────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    @contextmanager
    def _immediate(self):
        """BEGIN IMMEDIATE — serialises lease attempts across workers."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        with self._lock, self._conn() as c:
            # Idempotent column additions for existing DBs.
            for table, col, decl in [
                ("qa_plans",              "promote_ready", "INTEGER NOT NULL DEFAULT 0"),
                ("qa_tasks",              "not_before",    "TEXT"),  # spread pacing
                # New shape for auto-plan configs (open scenario filter +
                # debounce + pacing). Old unit_scenarios/full_scenarios
                # columns are left in place for back-compat with existing
                # rows; new code reads scenarios/scenario_tags first.
                ("qa_auto_plan_configs",  "scenarios",            "TEXT"),
                ("qa_auto_plan_configs",  "scenario_tags",        "TEXT"),
                ("qa_auto_plan_configs",  "bench_repo_path",      "TEXT"),
                ("qa_auto_plan_configs",  "min_gap_seconds",      "INTEGER NOT NULL DEFAULT 0"),
                ("qa_auto_plan_configs",  "pacing",               "TEXT NOT NULL DEFAULT 'aggressive'"),
                ("qa_auto_plan_configs",  "pacing_window_seconds","INTEGER NOT NULL DEFAULT 0"),
                # mode: 'auto' fires on new commits via DiscoverySupervisor;
                # 'on_demand' is hand-fired from UI/CLI per (lineage, sha).
                ("qa_auto_plan_configs",  "mode",                 "TEXT NOT NULL DEFAULT 'auto'"),
                # Per-pool subprocess timeout — DeepSeek runs can take
                # 10–15min; we cap each task's bench subprocess at this
                # so a stuck LLM doesn't hold a slot indefinitely.
                ("qa_worker_pools",       "task_timeout_seconds", "INTEGER NOT NULL DEFAULT 900"),
                # B-light judge phantom task tracking. kind='agent'
                # tasks are leased and run by workers; kind='judge'
                # tasks are companions that ride along with their
                # agent (same bench subprocess scores both) and are
                # finished automatically when the agent finishes —
                # surfaces "agents X/Y · judges A/B" progress in the
                # UI without a bench-side refactor.
                ("qa_tasks",              "kind",                 "TEXT NOT NULL DEFAULT 'agent'"),
                ("qa_tasks",              "parent_task_id",       "INTEGER"),
                # Auto-retry for transient errors. retry_count tracks how
                # many times this row has been re-queued from a failed
                # attempt; max_retries is the per-task ceiling (default
                # DEFAULT_MAX_RETRIES). finish() decides retry vs final.
                ("qa_tasks",              "retry_count",          "INTEGER NOT NULL DEFAULT 0"),
                ("qa_tasks",              "max_retries",          f"INTEGER NOT NULL DEFAULT {DEFAULT_MAX_RETRIES}"),
            ]:
                try:
                    c.execute(f"SELECT {col} FROM {table} LIMIT 0")
                except sqlite3.OperationalError:
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError:
                        # table doesn't exist yet — will be created by the
                        # CREATE TABLE block below.
                        pass
            # Rename branch → lineage on qa_tasks and qa_planned_commits;
            # rename branches → lineages on qa_plans. Idempotent: SELECT
            # the new name first, only ALTER if it errors.
            for table, old, new in [
                ("qa_tasks",            "branch",   "lineage"),
                ("qa_planned_commits",  "branch",   "lineage"),
                ("qa_plans",            "branches", "lineages"),
            ]:
                try:
                    c.execute(f"SELECT {new} FROM {table} LIMIT 0")
                except sqlite3.OperationalError:
                    try:
                        c.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                    except sqlite3.OperationalError:
                        pass  # table doesn't exist yet — initial CREATE will use new name
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_qa_plans_promote ON qa_plans(promote_ready)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_qa_tasks_not_before ON qa_tasks(not_before)")
            except sqlite3.OperationalError:
                pass
            c.executescript("""
                CREATE TABLE IF NOT EXISTS qa_tasks (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    state             TEXT NOT NULL DEFAULT 'queued',
                    kind              TEXT NOT NULL DEFAULT 'agent',
                    parent_task_id    INTEGER,
                    scenario_id       TEXT NOT NULL,
                    provider          TEXT NOT NULL,
                    attempt_n         INTEGER NOT NULL DEFAULT 1,
                    lineage           TEXT,
                    mutation_hash     TEXT,
                    plan_id           INTEGER,
                    priority          INTEGER NOT NULL DEFAULT 100,
                    payload           TEXT,
                    lease_owner       TEXT,
                    lease_expires_at  TEXT,
                    enqueued_at       TEXT NOT NULL,
                    started_at        TEXT,
                    finished_at       TEXT,
                    trace_run_id      TEXT,
                    result_json       TEXT,
                    error_class       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_state    ON qa_tasks(state);
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_provider ON qa_tasks(provider);
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_plan     ON qa_tasks(plan_id);
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_lease    ON qa_tasks(lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_kind     ON qa_tasks(kind, state);
                -- Direct (qa_tasks → runs) link via trace_run_id is the
                -- canonical join — no more mutation+time-window guessing.
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_trace_run ON qa_tasks(trace_run_id);
                -- Speed up sub-agent GROUP BY in /api/search/sub_runs.
                CREATE INDEX IF NOT EXISTS idx_events_run_agent  ON events(run_id, agent_id);
                -- Time-window scan is the primary axis for trace data
                -- (Jaeger/Tempo default to "last 1h"). Without these
                -- indexes our default ORDER BY timestamp DESC LIMIT N
                -- across millions of events does a full scan.
                CREATE INDEX IF NOT EXISTS idx_events_timestamp  ON events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_otel_spans_start  ON otel_spans(start_ns DESC);
                CREATE INDEX IF NOT EXISTS idx_qa_tasks_parent   ON qa_tasks(parent_task_id);

                CREATE TABLE IF NOT EXISTS qa_workers (
                    id                TEXT PRIMARY KEY,
                    pid               INTEGER,
                    provider          TEXT,
                    capacity          INTEGER NOT NULL DEFAULT 1,
                    started_at        TEXT NOT NULL,
                    last_heartbeat    TEXT NOT NULL,
                    state             TEXT NOT NULL DEFAULT 'running'
                );
                CREATE INDEX IF NOT EXISTS idx_qa_workers_provider ON qa_workers(provider);

                CREATE TABLE IF NOT EXISTS qa_auto_plan_configs (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                 TEXT,
                    repo_path            TEXT NOT NULL,
                    branch_pattern       TEXT NOT NULL,        -- glob
                    providers            TEXT NOT NULL,        -- JSON list
                    unit_scenarios       TEXT NOT NULL,        -- JSON list — every commit
                    full_scenarios       TEXT,                 -- JSON list — periodic
                    full_period_seconds  INTEGER DEFAULT 86400,-- min gap between full runs per lineage
                    attempts_min         INTEGER DEFAULT 1,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    created_at           TEXT NOT NULL,
                    last_discover_at     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qa_apc_enabled ON qa_auto_plan_configs(enabled);

                CREATE TABLE IF NOT EXISTS qa_worker_pools (
                    -- Server-side worker supervisor config: "keep N alive
                    -- workers for provider X while queue has tasks".
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT,
                    provider        TEXT NOT NULL,
                    target_workers  INTEGER NOT NULL DEFAULT 1,
                    trigger         TEXT NOT NULL DEFAULT 'live_queue',
                    -- 'live_queue' = spawn while queue non-empty for this provider
                    -- (future: 'cron', 'always_on')
                    max_idle_seconds INTEGER NOT NULL DEFAULT 120,
                    task_timeout_seconds INTEGER NOT NULL DEFAULT 900,
                    bench_cmd       TEXT,           -- override worker --bench-cmd template
                    enabled         INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL,
                    last_check_at   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qa_pools_enabled ON qa_worker_pools(enabled);
                CREATE INDEX IF NOT EXISTS idx_qa_pools_provider ON qa_worker_pools(provider);

                CREATE TABLE IF NOT EXISTS qa_planned_commits (
                    -- Idempotency ledger: one row per (config, lineage, sha,
                    -- plan_kind) ever planned. discover() consults this
                    -- before creating a plan; same (config, lineage, sha,
                    -- kind) never produces two plans, even after crash.
                    config_id      INTEGER NOT NULL,
                    lineage        TEXT NOT NULL,
                    sha            TEXT NOT NULL,
                    plan_kind      TEXT NOT NULL,           -- 'unit' | 'full'
                    plan_id        INTEGER NOT NULL,
                    planned_at     TEXT NOT NULL,
                    PRIMARY KEY (config_id, lineage, sha, plan_kind)
                );
                CREATE INDEX IF NOT EXISTS idx_qa_pc_lineage ON qa_planned_commits(lineage, planned_at DESC);

                CREATE TABLE IF NOT EXISTS qa_plans (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    name              TEXT,
                    created_at        TEXT NOT NULL,
                    created_by        TEXT,
                    lineages          TEXT,           -- JSON list
                    providers         TEXT,           -- JSON list
                    scenarios         TEXT,           -- JSON list
                    attempts_min      INTEGER NOT NULL DEFAULT 1,
                    state             TEXT NOT NULL DEFAULT 'running',
                    promote_ready     INTEGER NOT NULL DEFAULT 0,
                    notes             TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_qa_plans_state ON qa_plans(state);
                CREATE INDEX IF NOT EXISTS idx_qa_plans_promote ON qa_plans(promote_ready);
            """)
            c.commit()

    # ── Task CRUD ─────────────────────────────────────────────────────────

    def enqueue(self, spec: TaskSpec) -> int:
        """Insert a new task. Returns its id. Default state is 'queued';
        kind='judge' phantom companions should be created with
        initial_state='blocked' so lease() never picks them up."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_tasks
                   (state, kind, parent_task_id, scenario_id, provider,
                    attempt_n, lineage, mutation_hash, plan_id, priority,
                    payload, enqueued_at, not_before)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (spec.initial_state, spec.kind, spec.parent_task_id,
                 spec.scenario_id, spec.provider, spec.attempt_n,
                 spec.lineage or "", spec.mutation_hash or "",
                 spec.plan_id, spec.priority,
                 json.dumps(spec.payload, ensure_ascii=False),
                 _now().isoformat(),
                 spec.not_before),
            )
            c.commit()
            return int(cur.lastrowid)

    def get(self, task_id: int) -> Optional[TaskRow]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM qa_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list(self, *, state: Optional[str] = None,
             provider: Optional[str] = None,
             plan_id: Optional[int] = None,
             limit: int = 50, offset: int = 0) -> list[TaskRow]:
        clauses = ["1=1"]
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if plan_id is not None:
            clauses.append("plan_id=?")
            params.append(plan_id)
        params.extend([limit, offset])
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM qa_tasks WHERE {' AND '.join(clauses)} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ── Lease / heartbeat / finish ────────────────────────────────────────

    def lease(self, *, provider: str, worker_id: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Optional[TaskRow]:
        """Atomically pick the next queued task for `provider` and lease
        it to `worker_id`. Returns None if the queue is empty.

        Ordering: by `priority` (asc), then `enqueued_at` (asc) — so
        sentinel scenarios with low priority value come first.
        """
        now = _now()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        now_iso = now.isoformat()
        with self._lock, self._immediate() as c:
            row = c.execute(
                """SELECT id FROM qa_tasks
                   WHERE state='queued' AND provider=? AND kind='agent'
                     AND (not_before IS NULL OR not_before <= ?)
                   ORDER BY priority ASC, enqueued_at ASC
                   LIMIT 1""",
                (provider, now_iso),
            ).fetchone()
            if not row:
                return None
            task_id = int(row["id"])
            c.execute(
                """UPDATE qa_tasks
                   SET state='leased', lease_owner=?, lease_expires_at=?,
                       started_at=?
                   WHERE id=?""",
                (worker_id, expires, now.isoformat(), task_id),
            )
            updated = c.execute(
                "SELECT * FROM qa_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._row_to_task(updated) if updated else None

    def heartbeat(self, task_id: int, *, worker_id: str,
                  lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        """Extend lease_expires_at. Returns False if the task was
        already reaped or finished, so the worker knows to stop."""
        expires = (_now() + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE qa_tasks
                   SET lease_expires_at=?, state='running'
                   WHERE id=? AND lease_owner=? AND state IN ('leased', 'running')""",
                (expires, task_id, worker_id),
            )
            c.commit()
            return cur.rowcount > 0

    def finish(self, task_id: int, *, worker_id: str,
               state: str = "finished",
               trace_run_id: Optional[str] = None,
               result: Optional[dict] = None,
               error_class: Optional[str] = None) -> bool:
        """Terminal transition. Idempotent — second call overwrites.

        state in {finished, error, cancelled}.

        Auto-retry: when state=='error' and `error_class` is in
        `RETRYABLE_ERROR_CLASSES` and the task's retry_count is below
        max_retries, the row is re-queued instead of terminal-marked.
        retry_count is incremented and lease cleared. The bench cmd
        (including payload) is preserved.

        Side effect: if this was the last non-terminal task of a plan,
        the plan auto-transitions running → done.
        """
        if state not in ("finished", "error", "cancelled"):
            raise ValueError(f"invalid finish state: {state}")
        with self._lock, self._conn() as c:
            # Check retry eligibility for transient errors.
            retried = False
            if state == "error" and error_class in RETRYABLE_ERROR_CLASSES:
                row = c.execute(
                    "SELECT retry_count, max_retries FROM qa_tasks "
                    "WHERE id=? AND lease_owner=?",
                    (task_id, worker_id),
                ).fetchone()
                if row and int(row["retry_count"] or 0) < int(row["max_retries"] or 0):
                    c.execute(
                        """UPDATE qa_tasks
                           SET state='queued', retry_count = retry_count + 1,
                               lease_owner=NULL, lease_expires_at=NULL,
                               started_at=NULL, finished_at=NULL,
                               error_class=?, result_json=?
                           WHERE id=? AND lease_owner=?""",
                        (f"retry:{error_class}",
                         json.dumps(result, ensure_ascii=False)
                            if result is not None else None,
                         task_id, worker_id),
                    )
                    c.commit()
                    log.info("task %s retry %d/%d (error=%s)",
                             task_id, int(row["retry_count"]) + 1,
                             int(row["max_retries"]), error_class)
                    retried = True
            if retried:
                return True
            cur = c.execute(
                """UPDATE qa_tasks
                   SET state=?, finished_at=?, trace_run_id=?,
                       result_json=?, error_class=?
                   WHERE id=? AND lease_owner=?""",
                (state, _now().isoformat(), trace_run_id,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_class, task_id, worker_id),
            )
            c.commit()
            if cur.rowcount == 0:
                return False
            # Cascade to phantom judge: when an agent task terminates,
            # its companion judge task (kind='judge', parent_task_id=us)
            # was running in the same bench subprocess, so we mirror
            # the agent's terminal state onto it. If agent succeeded
            # → judge='finished'; otherwise judge='cancelled' (no
            # judge artefact to grade against a failed agent).
            judge_state = "finished" if state == "finished" else "cancelled"
            c.execute(
                """UPDATE qa_tasks
                   SET state=?, finished_at=?, trace_run_id=?,
                       error_class=?
                   WHERE parent_task_id=? AND kind='judge'
                     AND state IN ('blocked', 'queued')""",
                (judge_state, _now().isoformat(), trace_run_id,
                 error_class, task_id),
            )
            c.commit()
            # Look up plan_id of the just-finished task to drive the
            # plan-level auto-transition.
            plan_row = c.execute(
                "SELECT plan_id FROM qa_tasks WHERE id=?", (task_id,)
            ).fetchone()
        plan_id = plan_row["plan_id"] if plan_row else None
        if plan_id is not None:
            self._maybe_finish_plan(int(plan_id))
        return True

    def _maybe_finish_plan(self, plan_id: int) -> bool:
        """Transition plan running → done iff no non-terminal tasks
        remain. Cancelled plans are left as-is. Returns True if the
        plan was transitioned now.

        Note: promote_ready column on qa_plans is legacy/inert — the
        full-vs-unit framing was dropped in favour of open scenario-set
        schedules. Score-based promotion uses the per-mutation
        scoring axes (hard / soft / methodology) rather than a
        per-plan boolean.
        """
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT state FROM qa_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if not row or row["state"] != "running":
                return False
            non_terminal = c.execute(
                """SELECT COUNT(*) AS n FROM qa_tasks
                   WHERE plan_id=? AND state IN ('queued','leased','running')""",
                (plan_id,),
            ).fetchone()
            if int(non_terminal["n"] or 0) > 0:
                return False
            c.execute(
                "UPDATE qa_plans SET state='done' "
                "WHERE id=? AND state='running'",
                (plan_id,),
            )
            c.commit()
            return True

    def set_task_trace_run_id(self, task_id: int, trace_run_id: str) -> bool:
        """Stamp a task with its diff-graph run_id at lease time.

        Called by the worker right before spawning the bench process,
        so the (qa_tasks → runs) link is in the DB before the agent
        even emits its first event. Live observation can then show
        "task #N is run X" in real time, without time-window guessing.
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE qa_tasks SET trace_run_id=? WHERE id=?",
                (trace_run_id, task_id),
            )
            c.commit()
            return cur.rowcount > 0

    def cancel(self, task_id: int) -> bool:
        """Admin cancel — works on queued tasks too. Won't preempt a
        running task; the worker will discover via heartbeat=False."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE qa_tasks
                   SET state='cancelled', finished_at=?
                   WHERE id=? AND state IN ('queued', 'leased', 'running')""",
                (_now().isoformat(), task_id),
            )
            c.commit()
            return cur.rowcount > 0

    # ── Reaper ────────────────────────────────────────────────────────────

    def force_release_all_leases(self, *, reason: str = "force_release") -> int:
        """Unconditionally release every leased/running task back to
        queued, regardless of grace window. Used by the supervisor's
        sleep-resume handler: after host wake, every lease that was
        held during sleep is suspect — no worker actually ticked
        during that window. The bench subprocess may already be
        broken (network gone, LLM disconnected). Cheaper to retry
        than guess. Tasks pick up retry_count via subsequent finish()
        calls or via tooling; force_release itself doesn't bump it.
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE qa_tasks
                   SET state='queued', lease_owner=NULL, lease_expires_at=NULL,
                       started_at=NULL, error_class=?
                   WHERE state IN ('leased', 'running')""",
                (reason,),
            )
            c.commit()
            return cur.rowcount

    def reap_stale_leases(self, *, grace_seconds: int = DEFAULT_HEARTBEAT_GRACE_SECONDS) -> int:
        """Return tasks whose lease expired more than `grace_seconds`
        ago to state='queued'. Returns count of reaped tasks.

        Called at server startup (recover from kill -9 mid-run) and
        on demand via /qa/tasks/reap.
        """
        cutoff = (_now() - timedelta(seconds=grace_seconds)).isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE qa_tasks
                   SET state='queued', lease_owner=NULL, lease_expires_at=NULL,
                       started_at=NULL
                   WHERE state IN ('leased', 'running')
                     AND lease_expires_at IS NOT NULL
                     AND lease_expires_at < ?""",
                (cutoff,),
            )
            c.commit()
            return cur.rowcount

    # ── Workers ───────────────────────────────────────────────────────────

    def register_worker(self, *, worker_id: Optional[str] = None,
                        provider: str = "", capacity: int = 1,
                        pid: Optional[int] = None) -> str:
        wid = worker_id or str(uuid.uuid4())[:12]
        now = _now().isoformat()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO qa_workers
                   (id, pid, provider, capacity, started_at, last_heartbeat, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'running')""",
                (wid, pid, provider or "", capacity, now, now),
            )
            c.commit()
        return wid

    def worker_heartbeat(self, worker_id: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE qa_workers SET last_heartbeat=?, state='running' WHERE id=?",
                (_now().isoformat(), worker_id),
            )
            c.commit()
            return cur.rowcount > 0

    def worker_set_state(self, worker_id: str, state: str) -> bool:
        """Mark worker as stopped (clean exit) / dead (forcibly killed)."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE qa_workers SET state=?, last_heartbeat=? WHERE id=?",
                (state, _now().isoformat(), worker_id),
            )
            c.commit()
            return cur.rowcount > 0

    def list_workers(self,
                     stale_after_seconds: int = 90,
                     gc_terminal_after_seconds: int = 3600) -> list[dict]:
        """List worker fleet, self-healing on every call:

        - heartbeat > stale_after_seconds AND state='running' → state='dead'
        - state ∈ ('dead', 'stopped') AND heartbeat > gc_terminal_after_seconds
          → row deleted

        Server endpoint and CLI both call this, so dead/stopped workers
        auto-classify and auto-evict regardless of who's looking. Pass
        gc_terminal_after_seconds=0 to retain (no GC).
        """
        now = _now()
        stale_cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        gc_cutoff = (now - timedelta(seconds=gc_terminal_after_seconds)).isoformat()
        with self._lock, self._conn() as c:
            try:
                # Trace-as-heartbeat: a worker whose current task has a
                # run that emitted ANY event within the stale window is
                # alive — even if the heartbeat thread itself is wedged
                # (SQLite contention, GIL stall, …). This is a defense
                # in depth on top of the dedicated heartbeat thread.
                # We resurrect such workers by bumping last_heartbeat.
                c.execute(
                    """UPDATE qa_workers
                       SET last_heartbeat = ?
                       WHERE state='running' AND last_heartbeat < ?
                         AND id IN (
                           SELECT t.lease_owner
                           FROM qa_tasks t
                           JOIN runs r
                             ON r.scenario_id = t.scenario_id
                            AND r.mutation = t.mutation_hash
                           JOIN events e ON e.run_id = r.id
                           WHERE t.state IN ('leased', 'running')
                             AND t.lease_owner IS NOT NULL
                             AND e.timestamp > ?
                         )""",
                    (now.isoformat(), stale_cutoff, stale_cutoff),
                )
                c.execute(
                    "UPDATE qa_workers SET state='dead' "
                    "WHERE state='running' AND last_heartbeat < ?",
                    (stale_cutoff,),
                )
                # Release leased tasks owned by any worker currently in
                # a terminal state — otherwise tasks block on lease
                # expiry, which is later than dead-classification.
                c.execute(
                    """UPDATE qa_tasks
                       SET state='queued', lease_owner=NULL,
                           lease_expires_at=NULL, started_at=NULL
                       WHERE state IN ('leased', 'running')
                         AND lease_owner IN (
                           SELECT id FROM qa_workers
                           WHERE state IN ('dead', 'stopped')
                         )""",
                )
                if gc_terminal_after_seconds > 0:
                    c.execute(
                        "DELETE FROM qa_workers "
                        "WHERE state IN ('dead', 'stopped') AND last_heartbeat < ?",
                        (gc_cutoff,),
                    )
                c.commit()
            except Exception:
                pass
            rows = c.execute("SELECT * FROM qa_workers ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_task(row: sqlite3.Row | None) -> Optional[TaskRow]:
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except Exception:
            result = None
        return TaskRow(
            id=int(row["id"]),
            state=row["state"],
            scenario_id=row["scenario_id"],
            provider=row["provider"],
            attempt_n=int(row["attempt_n"]),
            lineage=row["lineage"] or "",
            mutation_hash=row["mutation_hash"] or "",
            plan_id=row["plan_id"],
            priority=int(row["priority"]),
            payload=payload,
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            enqueued_at=row["enqueued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            trace_run_id=row["trace_run_id"],
            result_json=result,
            error_class=row["error_class"],
            retry_count=int(row["retry_count"] or 0)
                if "retry_count" in row.keys() else 0,
            max_retries=int(row["max_retries"] or 0)
                if "max_retries" in row.keys() else 0,
        )


# ── Plans ────────────────────────────────────────────────────────────────────

@dataclass
class PlanSpec:
    """Inputs for create_plan — describes the QC matrix to enqueue.

    `lineages` = the list of long-lived lines of git commits we're
    testing (typically git branches like `master` or `feature/foo`).
    """
    name: str = ""
    created_by: str = ""
    lineages: list[str] = field(default_factory=list)         # ["feature/X", ...]
    providers: list[str] = field(default_factory=list)        # ["deepseek", "qwen3-6"]
    scenarios: list[str] = field(default_factory=list)        # ["REV-001", ...]
    attempts_min: int = 1
    priority: int = 100
    notes: str = ""


@dataclass
class PlanRow:
    id: int
    name: str
    created_at: str
    created_by: str
    lineages: list[str]
    providers: list[str]
    scenarios: list[str]
    attempts_min: int
    state: str
    notes: str
    promote_ready: bool = False


class PlanStore:
    """Plans live alongside tasks in the same DB. A plan is just an
    aggregate handle over N tasks (cross-product of lineages × providers
    × scenarios × attempts_min). Cancelling a plan soft-cancels its
    queued tasks; running tasks finish on their own.
    """

    def __init__(self, queue: TaskQueue):
        self.queue = queue

    def create_anonymous(self, *, name: str, scenarios: list[dict],
                          lineage: str, mutation_hash: str,
                          provider: str, attempts_min: int = 1,
                          priority: int = 100,
                          notes: str = "") -> tuple[int, list[int]]:
        """One-shot plan over a heterogeneous list of scenarios.

        `scenarios` is a list of dicts: {id, bench_cmd?} — per-scenario
        runner override goes into the task's payload. Used by
        /api/qa/fire-anonymous so unit-tier and integration scenarios
        can co-exist in a single plan and the worker dispatches each
        with the right cmd template.

        Phantom judge companion is still created for each agent task
        (matches auto-fire behaviour). Returns (plan_id, [task_ids]).
        """
        if not scenarios:
            raise ValueError("anonymous plan requires at least one scenario")
        if not provider:
            raise ValueError("anonymous plan requires a provider")
        scenario_ids = [str(s["id"]) for s in scenarios if s.get("id")]
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_plans
                   (name, created_at, created_by, lineages, providers,
                    scenarios, attempts_min, state, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (name or "", _now().isoformat(), "anonymous",
                 json.dumps([lineage] if lineage else []),
                 json.dumps([provider]),
                 json.dumps(scenario_ids),
                 max(1, attempts_min), notes or ""),
            )
            plan_id = int(cur.lastrowid)
            c.commit()
        task_ids: list[int] = []
        for s in scenarios:
            scen_id = str(s.get("id") or "")
            bench_cmd = str(s.get("bench_cmd") or "")
            payload_base: dict = {"plan_name": name} if name else {}
            if bench_cmd:
                payload_base["bench_cmd"] = bench_cmd
            for attempt_n in range(1, max(1, attempts_min) + 1):
                agent_tid = self.queue.enqueue(TaskSpec(
                    scenario_id=scen_id,
                    provider=provider,
                    attempt_n=attempt_n,
                    lineage=lineage or "",
                    mutation_hash=mutation_hash or "",
                    plan_id=plan_id,
                    priority=priority,
                    payload=dict(payload_base),
                ))
                task_ids.append(agent_tid)
                judge_tid = self.queue.enqueue(TaskSpec(
                    scenario_id=scen_id,
                    provider=provider,
                    attempt_n=attempt_n,
                    lineage=lineage or "",
                    mutation_hash=mutation_hash or "",
                    plan_id=plan_id,
                    priority=priority,
                    kind="judge",
                    parent_task_id=agent_tid,
                    initial_state="blocked",
                    payload=dict(payload_base),
                ))
                task_ids.append(judge_tid)
        return plan_id, task_ids

    def create(self, spec: PlanSpec) -> tuple[int, list[int]]:
        """Insert plan + fan-out into qa_tasks. Returns (plan_id, [task_ids]).

        Empty lineages → enqueue tasks without lineage (single-PR-less
        scenarios; the bench knows from scenario.id alone). Empty
        providers / scenarios → ValueError.
        """
        if not spec.providers:
            raise ValueError("plan requires at least one provider")
        if not spec.scenarios:
            raise ValueError("plan requires at least one scenario")

        now = _now().isoformat()
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_plans
                   (name, created_at, created_by, lineages, providers,
                    scenarios, attempts_min, state, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (spec.name or "", now, spec.created_by or "",
                 json.dumps(spec.lineages, ensure_ascii=False),
                 json.dumps(spec.providers, ensure_ascii=False),
                 json.dumps(spec.scenarios, ensure_ascii=False),
                 max(1, spec.attempts_min), spec.notes or ""),
            )
            plan_id = int(cur.lastrowid)
            c.commit()

        # Fan-out via the existing enqueue (each acquires the queue lock
        # individually, but at the speed of bulk inserts it's fine).
        task_ids: list[int] = []
        lineages = spec.lineages or [""]    # [""] → one task per (provider, scenario)
        for lineage in lineages:
            for provider in spec.providers:
                for scenario in spec.scenarios:
                    for attempt_n in range(1, max(1, spec.attempts_min) + 1):
                        # Agent task — leased + run by a worker.
                        agent_tid = self.queue.enqueue(TaskSpec(
                            scenario_id=scenario,
                            provider=provider,
                            attempt_n=attempt_n,
                            lineage=lineage,
                            plan_id=plan_id,
                            priority=spec.priority,
                            payload={"plan_name": spec.name} if spec.name else {},
                        ))
                        task_ids.append(agent_tid)
                        # Phantom judge companion — never leased; the
                        # bench process scoring the agent IS the judge,
                        # so finish() of the agent cascades the same
                        # state onto this row. Adds visibility "agents
                        # X/Y · judges A/B" without a bench refactor.
                        judge_tid = self.queue.enqueue(TaskSpec(
                            scenario_id=scenario,
                            provider=provider,
                            attempt_n=attempt_n,
                            lineage=lineage,
                            plan_id=plan_id,
                            priority=spec.priority,
                            kind="judge",
                            parent_task_id=agent_tid,
                            initial_state="blocked",
                            payload={"plan_name": spec.name} if spec.name else {},
                        ))
                        task_ids.append(judge_tid)
        return plan_id, task_ids

    def get(self, plan_id: int) -> Optional[PlanRow]:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                "SELECT * FROM qa_plans WHERE id=?", (plan_id,)
            ).fetchone()
        return self._row_to_plan(row) if row else None

    def live_score(self, plan_id: int) -> dict:
        """Running mean of judge overall_score across judge runs whose
        time-window matches a task in this plan.

        Match join is the same as /api/search/runs?plan=N — by mutation
        + task.started_at..finished_at window. Score is parsed from the
        judge's `agent_llm_response` event payload (data.overall_score
        or data.data.overall_score).

        Returns:
          {"n": int, "mean": float|None, "last": [{"score": float,
           "scenario": str, "ts": iso}, …]}  -- last 5 in chrono order.
        """
        out = {"n": 0, "mean": None, "last": []}
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                """
                SELECT r.id, r.scenario_id, r.finished_at
                FROM runs r
                WHERE r.kind='judge' AND r.status='completed'
                  AND r.linked_run_id IN (
                    -- agent runs that belong to this plan
                    SELECT a.id FROM runs a JOIN qa_tasks t
                      ON SUBSTR(t.mutation_hash, 1, 7) = a.mutation
                     AND t.started_at IS NOT NULL
                     AND a.started_at >= t.started_at
                     AND a.started_at <= COALESCE(t.finished_at, datetime('now'))
                    WHERE t.plan_id = ?
                  )
                ORDER BY r.finished_at DESC LIMIT 200
                """,
                (plan_id,),
            ).fetchall()
            if not rows:
                return out
            judge_ids = [r["id"] for r in rows]
            placeholders = ",".join("?" for _ in judge_ids)
            ev_rows = c.execute(
                f"""SELECT run_id, data_json FROM events
                    WHERE event_type='agent_llm_response'
                      AND run_id IN ({placeholders})""",
                judge_ids,
            ).fetchall()
        scores_by_id: dict[str, float] = {}
        for r in ev_rows:
            try:
                d = json.loads(r["data_json"])
                inner = d.get("data") or d.get("content") or {}
                if isinstance(inner, str):
                    inner = json.loads(inner)
                if isinstance(inner, dict) and "overall_score" in inner:
                    scores_by_id[r["run_id"]] = float(inner["overall_score"])
            except Exception:
                continue
        if not scores_by_id:
            return out
        scores = list(scores_by_id.values())
        out["n"] = len(scores)
        out["mean"] = round(sum(scores) / len(scores), 3)
        # Tail: most recent 5, in chrono order (oldest first).
        tail = []
        for r in rows[:5]:
            s = scores_by_id.get(r["id"])
            if s is None:
                continue
            tail.append({"score": round(s, 3),
                         "scenario": r["scenario_id"] or "",
                         "ts": r["finished_at"]})
        out["last"] = list(reversed(tail))
        return out

    def list(self, *, state: Optional[str] = None,
             limit: int = 50, offset: int = 0) -> list[PlanRow]:
        clauses, params = ["1=1"], []
        if state:
            clauses.append("state=?")
            params.append(state)
        params.extend([limit, offset])
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                f"SELECT * FROM qa_plans WHERE {' AND '.join(clauses)} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._row_to_plan(r) for r in rows]

    def count(self, *, state: Optional[str] = None) -> int:
        clauses, params = ["1=1"], []
        if state:
            clauses.append("state=?")
            params.append(state)
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) AS n FROM qa_plans WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row["n"]) if row else 0

    def progress(self, plan_id: int) -> dict:
        """Aggregate stats across the plan's tasks. Top-level keys are
        the union of states across both kinds (back-compat for callers
        that just want overall counts); `by_kind` breaks out agents vs
        phantom judges so the UI can show "agents X/Y · judges A/B".
        """
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                """SELECT kind, state, COUNT(*) AS n
                   FROM qa_tasks WHERE plan_id=?
                   GROUP BY kind, state""",
                (plan_id,),
            ).fetchall()
        out: dict = {}
        by_kind: dict[str, dict] = {}
        for r in rows:
            k = r["kind"] or "agent"
            slot = by_kind.setdefault(k, {})
            slot[r["state"]] = int(r["n"])
            out[r["state"]] = out.get(r["state"], 0) + int(r["n"])
        for slot in by_kind.values():
            slot["total"] = sum(slot.values())
        out["total"] = sum(v for v in out.values() if isinstance(v, int))
        out["by_kind"] = by_kind
        return out

    def eta(self, plan_id: int) -> dict:
        """ETA for a plan based on historical durations of analogous
        (scenario × provider) runs.

        Strategy:
        - For each task, look up avg(duration_ms) across all
          finished agent runs with the same (scenario_id, provider).
          Falls back to per-provider mean, then to a 60s default
          when nothing is known.
        - For 'leased' / 'running' tasks, subtract elapsed time
          from the estimated total (clamped to ≥ 1s).
        - Per-provider concurrency comes from qa_worker_pools'
          target_workers (or 1 if no pool exists for the provider).
        - Plan ETA = max across providers of (remaining_seconds /
          parallel_workers).

        Returns:
          {
            "remaining_tasks": int,
            "eta_seconds": int | None,
            "eta_at":      iso8601 | None,
            "per_provider": [
                {"provider": str, "remaining_tasks": int,
                 "remaining_seconds": int, "workers": int,
                 "eta_seconds": int}, …
            ],
            "based_on": {"history_runs": int, "fallback_default_s": 60}
          }
        """
        now = _now()
        with self.queue._lock, self.queue._conn() as c:
            tasks = c.execute(
                """SELECT id, scenario_id, provider, state, started_at
                   FROM qa_tasks
                   WHERE plan_id=? AND kind='agent'""",
                (plan_id,),
            ).fetchall()
            if not tasks:
                return {"remaining_tasks": 0, "eta_seconds": 0,
                        "eta_at": now.isoformat(timespec="seconds"),
                        "per_provider": [],
                        "based_on": {"history_runs": 0, "fallback_default_s": 60}}

            # Historical averages — agent runs only.
            # Note: runs.model is the LLM model name (e.g. 'deepseek-chat')
            # while qa_tasks.provider is the user-facing key ('deepseek').
            # We match by scenario only — model is too noisy across configs
            # — and fall back gracefully when a scenario has no history.
            hist_rows = c.execute(
                """SELECT scenario_id,
                          AVG(duration_ms) AS avg_ms,
                          COUNT(*) AS n
                   FROM runs
                   WHERE kind='agent' AND status='completed'
                     AND duration_ms IS NOT NULL
                     AND scenario_id IS NOT NULL
                   GROUP BY scenario_id""",
            ).fetchall()
            by_scenario: dict[str, float] = {
                r["scenario_id"]: float(r["avg_ms"]) for r in hist_rows
            }
            global_row = c.execute(
                """SELECT AVG(duration_ms) AS avg_ms, COUNT(*) AS n
                   FROM runs
                   WHERE kind='agent' AND status='completed'
                     AND duration_ms IS NOT NULL""",
            ).fetchone()
            global_avg_ms = float(global_row["avg_ms"]) if global_row and global_row["avg_ms"] else None
            history_runs_total = int(global_row["n"]) if global_row else 0

            # Concurrency policy from pools.
            pool_rows = c.execute(
                """SELECT provider, target_workers
                   FROM qa_worker_pools WHERE enabled=1""",
            ).fetchall()
            workers_by_provider: dict[str, int] = {
                r["provider"]: max(1, int(r["target_workers"])) for r in pool_rows
            }

        FALLBACK_MS = 60_000.0

        def _est_total_ms(scen: str, prov: str) -> float:
            return (by_scenario.get(scen)
                    or global_avg_ms
                    or FALLBACK_MS)

        terminal = {"finished", "cancelled", "error"}
        per_prov: dict[str, dict] = {}
        remaining_total = 0
        for t in tasks:
            if t["state"] in terminal:
                continue
            prov = t["provider"] or "?"
            scen = t["scenario_id"] or ""
            est = _est_total_ms(scen, prov)
            if t["state"] in ("leased", "running") and t["started_at"]:
                try:
                    started = datetime.fromisoformat(t["started_at"])
                    elapsed = max(0.0, (now - started).total_seconds() * 1000.0)
                    remaining_ms = max(1000.0, est - elapsed)
                except Exception:
                    remaining_ms = est
            else:
                remaining_ms = est
            slot = per_prov.setdefault(prov, {"provider": prov,
                                              "remaining_tasks": 0,
                                              "remaining_ms": 0.0})
            slot["remaining_tasks"] += 1
            slot["remaining_ms"] += remaining_ms
            remaining_total += 1

        per_provider_out = []
        plan_eta_s = 0
        for prov, slot in per_prov.items():
            workers = workers_by_provider.get(prov, 1)
            eta_s = int(round(slot["remaining_ms"] / 1000.0 / max(1, workers)))
            per_provider_out.append({
                "provider": prov,
                "remaining_tasks": slot["remaining_tasks"],
                "remaining_seconds": int(round(slot["remaining_ms"] / 1000.0)),
                "workers": workers,
                "eta_seconds": eta_s,
            })
            plan_eta_s = max(plan_eta_s, eta_s)

        eta_at = (now + timedelta(seconds=plan_eta_s)
                  ).isoformat(timespec="seconds") if remaining_total else now.isoformat(timespec="seconds")

        return {
            "remaining_tasks": remaining_total,
            "eta_seconds": plan_eta_s if remaining_total else 0,
            "eta_at": eta_at,
            "per_provider": sorted(per_provider_out,
                                   key=lambda x: -x["eta_seconds"]),
            "based_on": {"history_runs": history_runs_total,
                         "fallback_default_s": int(FALLBACK_MS / 1000)},
        }

    def cancel(self, plan_id: int) -> int:
        """Soft-cancel: mark plan + its queued tasks (incl. blocked
        judge phantoms) as cancelled. Returns count of tasks cancelled."""
        with self.queue._lock, self.queue._conn() as c:
            c.execute("UPDATE qa_plans SET state='cancelled' WHERE id=?", (plan_id,))
            cur = c.execute(
                """UPDATE qa_tasks SET state='cancelled', finished_at=?
                   WHERE plan_id=?
                     AND state IN ('queued', 'leased', 'running', 'blocked')""",
                (_now().isoformat(), plan_id),
            )
            c.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_plan(row: sqlite3.Row | None) -> Optional[PlanRow]:
        if row is None:
            return None
        try:
            lineages = json.loads(row["lineages"]) if row["lineages"] else []
        except Exception:
            lineages = []
        try:
            providers = json.loads(row["providers"]) if row["providers"] else []
        except Exception:
            providers = []
        try:
            scenarios = json.loads(row["scenarios"]) if row["scenarios"] else []
        except Exception:
            scenarios = []
        # promote_ready may be missing on rows from pre-migration DBs.
        try:
            promote_ready = bool(row["promote_ready"]) if row["promote_ready"] is not None else False
        except (KeyError, IndexError):
            promote_ready = False
        return PlanRow(
            id=int(row["id"]),
            name=row["name"] or "",
            created_at=row["created_at"],
            created_by=row["created_by"] or "",
            lineages=lineages,
            providers=providers,
            scenarios=scenarios,
            attempts_min=int(row["attempts_min"]),
            state=row["state"],
            notes=row["notes"] or "",
            promote_ready=promote_ready,
        )


def plan_to_dict(p: PlanRow, *, progress: dict | None = None,
                 eta: dict | None = None,
                 live_score: dict | None = None) -> dict:
    out = {
        "id": p.id,
        "name": p.name,
        "created_at": p.created_at,
        "created_by": p.created_by,
        "lineages": p.lineages,
        "providers": p.providers,
        "scenarios": p.scenarios,
        "attempts_min": p.attempts_min,
        "state": p.state,
        "promote_ready": p.promote_ready,
        "notes": p.notes,
    }
    if progress is not None:
        out["progress"] = progress
    if eta is not None:
        out["eta"] = eta
    if live_score is not None:
        out["live_score"] = live_score
    return out


def task_to_dict(t: TaskRow) -> dict:
    """For API serialisation. JSON-safe.

    `resources` lists URIs the task references (scenario://X,
    provider://Y, lineage://Z, mutation://abc). They're synthesised
    from the legacy QA columns so old + new callers see one format —
    UI / CLI can deep-link without per-column glue.
    """
    res: list[str] = []
    if t.scenario_id:    res.append(f"scenario://{t.scenario_id}")
    if t.provider:       res.append(f"provider://{t.provider}")
    if t.lineage:        res.append(f"lineage://{t.lineage}")
    if t.mutation_hash:  res.append(f"mutation://{t.mutation_hash}")
    if t.plan_id:        res.append(f"plan://{t.plan_id}")
    if t.trace_run_id:   res.append(f"session://{t.trace_run_id}")
    # Payload may carry caller-supplied resource URIs (from the
    # generic enqueue path). Merge, de-duplicate, preserve order.
    payload_resources = (t.payload or {}).get("resources") or []
    seen = set(res)
    for u in payload_resources:
        if u not in seen:
            res.append(u)
            seen.add(u)
    return {
        "id": t.id,
        "state": t.state,
        # legacy QA fields — kept for now; new clients should
        # prefer `resources` for any QA-bound semantics.
        "scenario_id": t.scenario_id,
        "provider": t.provider,
        "attempt_n": t.attempt_n,
        "lineage": t.lineage,
        "mutation_hash": t.mutation_hash,
        "plan_id": t.plan_id,
        # generic, abstract task fields.
        "queue": t.provider,                     # alias — `queue` is the right
                                                 # generic name; `provider`
                                                 # stays for legacy clients.
        "priority": t.priority,
        "payload": t.payload,
        "resources": res,
        "tags": list((t.payload or {}).get("tags") or []),
        "retry_count": t.retry_count,
        "max_retries": t.max_retries,
        "lease_owner": t.lease_owner,
        "lease_expires_at": t.lease_expires_at,
        "enqueued_at": t.enqueued_at,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "trace_run_id": t.trace_run_id,
        "result_json": t.result_json,
        "error_class": t.error_class,
    }
