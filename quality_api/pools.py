"""Worker pool supervisor — server-side process manager for workers.

A pool config says: "keep N alive workers for provider X while queue
has tasks for X". The supervisor runs as a background asyncio task
inside the FastAPI server; every check_interval_seconds it counts
alive workers per provider and spawns new ones (as subprocesses)
when below target.

Workers spawned this way exit on their own via --max-idle-seconds
when the queue dries up — pool naturally scales to zero.

Why subprocess (not asyncio.subprocess threads): the worker invokes
the bench CLI which itself uses subprocess + asyncio internally.
Running them in the same FastAPI event loop is too tangled. Each
worker runs as an OS process, isolated, can be SIGKILL'd, exits
cleanly via heartbeat.

Triggers (extensible):
- 'live_queue' (current default) — spawn while queue.has_queued(provider).
- 'always_on' (future) — keep target_workers regardless.
- 'cron' (future) — schedule-based spawn at specific times.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .queue import TaskQueue

log = logging.getLogger(__name__)


@dataclass
class PoolConfig:
    id: int
    name: str
    queue: str
    target_workers: int
    trigger: str
    max_idle_seconds: int
    task_timeout_seconds: int
    bench_cmd: str
    enabled: bool
    created_at: str
    last_check_at: Optional[str]


def _row_to_pool(row) -> Optional[PoolConfig]:
    if row is None:
        return None
    def _col(name, default=None):
        try:
            v = row[name]
            return v if v is not None else default
        except (KeyError, IndexError):
            return default
    return PoolConfig(
        id=int(row["id"]),
        name=row["name"] or "",
        queue=row["queue"],
        target_workers=int(row["target_workers"] or 1),
        trigger=row["trigger"] or "live_queue",
        max_idle_seconds=int(row["max_idle_seconds"] or 120),
        task_timeout_seconds=int(_col("task_timeout_seconds", 900) or 900),
        bench_cmd=_col("bench_cmd", "") or "",
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_check_at=row["last_check_at"],
    )


def pool_to_dict(p: PoolConfig) -> dict:
    return {
        "id": p.id, "name": p.name, "queue": p.queue,
        "target_workers": p.target_workers, "trigger": p.trigger,
        "max_idle_seconds": p.max_idle_seconds,
        "task_timeout_seconds": p.task_timeout_seconds,
        "bench_cmd": p.bench_cmd,
        "enabled": p.enabled, "created_at": p.created_at,
        "last_check_at": p.last_check_at,
    }


class PoolStore:
    """CRUD over qa_worker_pools."""

    def __init__(self, queue: TaskQueue):
        self.queue = queue

    def add(self, *, name: str, queue: str, target_workers: int = 1,
            trigger: str = "live_queue",
            max_idle_seconds: int = 120,
            task_timeout_seconds: int = 900,
            bench_cmd: str = "",
            enabled: bool = True) -> int:
        if not queue:
            raise ValueError("queue required")
        if target_workers < 1:
            raise ValueError("target_workers must be >= 1")
        if trigger not in ("live_queue",):
            raise ValueError(f"unknown trigger {trigger}")
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_worker_pools
                   (name, queue, target_workers, trigger,
                    max_idle_seconds, task_timeout_seconds,
                    bench_cmd, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name or "", queue, int(target_workers), trigger,
                 int(max_idle_seconds), int(task_timeout_seconds),
                 bench_cmd or "",
                 1 if enabled else 0, datetime.now(timezone.utc).isoformat()),
            )
            c.commit()
            return int(cur.lastrowid)

    def update(self, pool_id: int, **fields) -> bool:
        allowed = {"name", "queue", "target_workers", "trigger",
                   "max_idle_seconds", "task_timeout_seconds",
                   "bench_cmd", "enabled"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return False
        params.append(pool_id)
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                f"UPDATE qa_worker_pools SET {', '.join(sets)} WHERE id=?",
                params,
            )
            c.commit()
            return cur.rowcount > 0

    def delete(self, pool_id: int) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute("DELETE FROM qa_worker_pools WHERE id=?", (pool_id,))
            c.commit()
            return cur.rowcount > 0

    def list(self, *, only_enabled: bool = False) -> list[PoolConfig]:
        clause = "WHERE enabled=1" if only_enabled else ""
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                f"SELECT * FROM qa_worker_pools {clause} ORDER BY id"
            ).fetchall()
        return [r for r in (_row_to_pool(x) for x in rows) if r is not None]

    def get(self, pool_id: int) -> Optional[PoolConfig]:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                "SELECT * FROM qa_worker_pools WHERE id=?", (pool_id,)
            ).fetchone()
        return _row_to_pool(row)


# ── Supervisor ──────────────────────────────────────────────────────────────

from . import config as _qa_config

# Lazily resolved — re-evaluated on each access via the property-
# like functions below so tests / runtime env changes propagate.
def _default_bench_cmd() -> str:
    return _qa_config.default_bench_cmd_template()

def _quality_cli_path() -> str:
    return _qa_config.quality_cli_path()

# Module-level aliases for callers that already imported by name.
# Resolve at import time too — supervisor uses these.
DEFAULT_BENCH_CMD = _default_bench_cmd()
QUALITY_CLI_PATH = _quality_cli_path()


class WorkerSupervisor:
    """Background loop that ensures each enabled pool has its target
    number of alive workers, spawning subprocesses as needed.

    Also handles fleet stability:
    - periodic reap of stale leases on every tick (~30 s), so a
      worker that died silently / went to sleep doesn't pin a task
    - sleep-resume detection via monotonic vs wall-clock drift —
      after host sleep, force-release ALL leases so tasks resume
      cleanly on the next tick instead of waiting for grace timeout
    """

    def __init__(self, queue: TaskQueue, store: PoolStore,
                 check_interval_seconds: int = 30):
        self.queue = queue
        self.store = store
        self.check_interval = check_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # PIDs we've spawned (best-effort accounting; workers self-evict
        # via DB heartbeat when they die).
        self._spawned: set[int] = set()
        # Sleep-resume sentinel: compare monotonic delta (paused
        # while system sleeps) vs wall-clock delta (advances during
        # sleep). A jump > threshold means the host slept since the
        # last tick.
        self._mono_anchor: Optional[float] = None
        self._wall_anchor: Optional[float] = None
        # Threshold above which we consider it a sleep-wake (vs.
        # normal scheduling jitter). At check_interval=30s the
        # expected drift is < 1s; 60s gives a wide margin.
        self._sleep_drift_threshold = 60.0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._tick)
            except Exception:
                log.exception("supervisor tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.check_interval,
                )
            except asyncio.TimeoutError:
                pass

    def _detect_sleep_wake(self) -> bool:
        """Compare wall vs monotonic clock since last tick. Returns
        True if the difference suggests the host slept (drift >
        threshold). Updates the anchors as a side effect."""
        import time
        now_mono = time.monotonic()
        now_wall = time.time()
        if self._mono_anchor is None:
            self._mono_anchor = now_mono
            self._wall_anchor = now_wall
            return False
        expected_delta = now_mono - self._mono_anchor
        actual_delta = now_wall - self._wall_anchor
        drift = actual_delta - expected_delta
        self._mono_anchor = now_mono
        self._wall_anchor = now_wall
        return drift > self._sleep_drift_threshold

    def _tick(self) -> None:
        """One supervision pass — runs in a thread (sync DB access).

        Sequence:
        1. Sleep-resume detection — if host slept since last tick,
           force-release all leases.
        2. Periodic reap — pick up any leases that expired naturally
           (worker died silently, GC pause exceeded grace).
        3. List workers — auto-heal pass marks stale workers `dead`
           and releases their tasks.
        4. Pool reconcile — spawn replacement workers if queue has
           queued tasks and we're below target.
        """
        if self._detect_sleep_wake():
            log.warning("supervisor: sleep-resume detected, force-releasing "
                        "all live leases")
            try:
                n = self.queue.force_release_all_leases(
                    reason="sleep_wake")
                if n > 0:
                    log.warning("supervisor: released %d leases held across sleep", n)
            except Exception:
                log.exception("supervisor: force_release_all_leases failed")
        try:
            n = self.queue.reap_stale_leases()
            if n > 0:
                log.info("supervisor: reaped %d stale leases", n)
        except Exception:
            log.exception("supervisor: reap_stale_leases failed")
        try:
            self.queue.list_workers()   # self-heal pass (stale → dead)
        except Exception:
            log.exception("supervisor: list_workers self-heal failed")

        for pool in self.store.list(only_enabled=True):
            self._reconcile(pool)
            with self.queue._lock, self.queue._conn() as c:
                c.execute(
                    "UPDATE qa_worker_pools SET last_check_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), pool.id),
                )
                c.commit()

    def _reconcile(self, pool: PoolConfig) -> None:
        if pool.trigger != "live_queue":
            return
        # Count queued tasks for this queue.
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n FROM qa_tasks
                   WHERE state='queued' AND queue=? AND kind='agent'
                     AND (not_before IS NULL OR not_before <= ?)""",
                (pool.queue, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            queued = int(row["n"] or 0)
        if queued == 0:
            return  # nothing to spawn for; existing workers will idle-out
        # Count alive workers subscribed to this queue.
        alive = sum(
            1 for w in self.queue.list_workers()
            if (w.get("queue") == pool.queue
                and (w.get("state") or "").strip().lower() == "running")
        )
        deficit = pool.target_workers - alive
        if deficit <= 0:
            return
        for _ in range(deficit):
            self._spawn_worker(pool)

    def _spawn_worker(self, pool: PoolConfig) -> None:
        """Fire-and-forget subprocess. Worker writes to the same DB and
        manages its own lifecycle via --max-idle-seconds."""
        bench_cmd = pool.bench_cmd or DEFAULT_BENCH_CMD
        cmd = (
            f"{QUALITY_CLI_PATH} --json worker "
            f"--queue={shlex.quote(pool.queue)} "
            f"--max-idle-seconds={pool.max_idle_seconds} "
            f"--task-timeout-seconds={pool.task_timeout_seconds} "
            f"--max-tasks=0 "
            f"--bench-cmd={shlex.quote(bench_cmd)}"
        )
        try:
            log_dir = Path("/tmp")
            log_path = log_dir / f"qa-pool-{pool.queue}-{os.getpid()}-{len(self._spawned)}.log"
            with open(log_path, "ab") as logfh:
                p = subprocess.Popen(
                    ["bash", "-c", cmd],
                    stdout=logfh, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self._spawned.add(p.pid)
            log.info("supervisor: spawned worker pid=%s for pool=%s queue=%s",
                     p.pid, pool.id, pool.queue)
        except Exception:
            log.exception("supervisor: failed to spawn worker for pool %s", pool.id)
