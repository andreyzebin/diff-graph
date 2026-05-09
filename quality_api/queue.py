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
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from orchestra.trace_db import DEFAULT_DB_PATH


DEFAULT_LEASE_SECONDS = 60
DEFAULT_HEARTBEAT_GRACE_SECONDS = 30


# ── Models ───────────────────────────────────────────────────────────────────

@dataclass
class TaskSpec:
    """What a worker needs to actually run a task. Caller-supplied."""
    scenario_id: str
    provider: str
    attempt_n: int = 1
    branch: str = ""
    mutation_hash: str = ""
    plan_id: Optional[int] = None
    priority: int = 100              # lower = sooner
    payload: dict = field(default_factory=dict)


@dataclass
class TaskRow:
    """Persisted shape — what /qa/tasks endpoints return."""
    id: int
    state: str
    scenario_id: str
    provider: str
    attempt_n: int
    branch: str
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
            c.executescript("""
                CREATE TABLE IF NOT EXISTS qa_tasks (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    state             TEXT NOT NULL DEFAULT 'queued',
                    scenario_id       TEXT NOT NULL,
                    provider          TEXT NOT NULL,
                    attempt_n         INTEGER NOT NULL DEFAULT 1,
                    branch            TEXT,
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
            """)
            c.commit()

    # ── Task CRUD ─────────────────────────────────────────────────────────

    def enqueue(self, spec: TaskSpec) -> int:
        """Insert a new task in state='queued'. Returns its id."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_tasks
                   (state, scenario_id, provider, attempt_n, branch,
                    mutation_hash, plan_id, priority, payload, enqueued_at)
                   VALUES ('queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (spec.scenario_id, spec.provider, spec.attempt_n,
                 spec.branch or "", spec.mutation_hash or "",
                 spec.plan_id, spec.priority,
                 json.dumps(spec.payload, ensure_ascii=False),
                 datetime.now().isoformat()),
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
        now = datetime.now()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._immediate() as c:
            row = c.execute(
                """SELECT id FROM qa_tasks
                   WHERE state='queued' AND provider=?
                   ORDER BY priority ASC, enqueued_at ASC
                   LIMIT 1""",
                (provider,),
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
        expires = (datetime.now() + timedelta(seconds=lease_seconds)).isoformat()
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
        """
        if state not in ("finished", "error", "cancelled"):
            raise ValueError(f"invalid finish state: {state}")
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE qa_tasks
                   SET state=?, finished_at=?, trace_run_id=?,
                       result_json=?, error_class=?
                   WHERE id=? AND lease_owner=?""",
                (state, datetime.now().isoformat(), trace_run_id,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_class, task_id, worker_id),
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
                (datetime.now().isoformat(), task_id),
            )
            c.commit()
            return cur.rowcount > 0

    # ── Reaper ────────────────────────────────────────────────────────────

    def reap_stale_leases(self, *, grace_seconds: int = DEFAULT_HEARTBEAT_GRACE_SECONDS) -> int:
        """Return tasks whose lease expired more than `grace_seconds`
        ago to state='queued'. Returns count of reaped tasks.

        Called at server startup (recover from kill -9 mid-run) and
        on demand via /qa/tasks/reap.
        """
        cutoff = (datetime.now() - timedelta(seconds=grace_seconds)).isoformat()
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
        now = datetime.now().isoformat()
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
                (datetime.now().isoformat(), worker_id),
            )
            c.commit()
            return cur.rowcount > 0

    def list_workers(self) -> list[dict]:
        with self._lock, self._conn() as c:
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
            branch=row["branch"] or "",
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
        )


def task_to_dict(t: TaskRow) -> dict:
    """For API serialisation. JSON-safe."""
    return {
        "id": t.id,
        "state": t.state,
        "scenario_id": t.scenario_id,
        "provider": t.provider,
        "attempt_n": t.attempt_n,
        "branch": t.branch,
        "mutation_hash": t.mutation_hash,
        "plan_id": t.plan_id,
        "priority": t.priority,
        "payload": t.payload,
        "lease_owner": t.lease_owner,
        "lease_expires_at": t.lease_expires_at,
        "enqueued_at": t.enqueued_at,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "trace_run_id": t.trace_run_id,
        "result_json": t.result_json,
        "error_class": t.error_class,
    }
