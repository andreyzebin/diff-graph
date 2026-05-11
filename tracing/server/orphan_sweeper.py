"""
Reactive orphan-run sweeper.

When `cli.py run` exits cleanly (or via the new SIGTERM-aware
try/finally), it marks its `runs` row as `completed` / `failed` and
the surrounding `cli.session` OTel span closes. SIGKILL, OOM-kill,
power loss, and host reboots dodge that finalisation — the row stays
at `status='running', finished_at=NULL` forever. Those orphans clog
the trace UI ("8 runs still running" — really, none are).

The sweeper is the defence-in-depth layer for that: a background
asyncio task that scans every minute for rows older than the
configured threshold and force-completes them as `failed`. Mirror of
`DiscoverySupervisor` / `WorkerSupervisor` in shape — same lifecycle
hooks, same DB-as-truth contract.

It uses BOTH a wall-clock threshold (`started_at` older than
`max_run_age_seconds` — default 1h) AND an activity check (no spans
written in the last `idle_grace_seconds` — default 10m). Real unit
runs finish in tens of seconds; integration runs in minutes; the
thresholds leave plenty of headroom. The activity check is the
cheap path: if spans are still arriving for a run, leave it alone
even if it's been going a long time.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


def _sweep_once(
    db_path: str,
    *,
    max_run_age_seconds: int,
    idle_grace_seconds: int,
) -> int:
    """One sweep. Returns the number of rows force-completed."""
    now = datetime.datetime.now(datetime.timezone.utc)
    age_cutoff = (now - datetime.timedelta(seconds=max_run_age_seconds)).isoformat()
    # Idle cutoff in nanoseconds (matches otel_spans.start_ns column).
    idle_cutoff_ns = int(
        (now - datetime.timedelta(seconds=idle_grace_seconds)).timestamp() * 1e9
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        # Two-step: find candidate run ids, then close only those.
        # SQLite's UPDATE with a NOT-EXISTS subquery on a multi-million
        # row spans table is slow; this split is faster + auditable.
        candidates = conn.execute(
            """
            SELECT id FROM runs
             WHERE status='running'
               AND started_at IS NOT NULL
               AND started_at < ?
            """,
            (age_cutoff,),
        ).fetchall()
        if not candidates:
            return 0

        closed = 0
        for (run_id,) in candidates:
            # Has any span fired for this run in the activity window?
            row = conn.execute(
                """
                SELECT 1 FROM otel_spans
                 WHERE start_ns > ?
                   AND attributes LIKE ?
                 LIMIT 1
                """,
                (idle_cutoff_ns, f'%"diffgraph.run_id": "{run_id}"%'),
            ).fetchone()
            if row:
                # Still active — leave alone.
                continue
            conn.execute(
                """
                UPDATE runs
                   SET status='failed',
                       finished_at=COALESCE(finished_at, ?)
                 WHERE id=? AND status='running'
                """,
                (now.isoformat(), run_id),
            )
            # Synthetic terminal event — without one the UI shows a
            # tool_call/llm_request with no closing arrow and the user
            # can't tell "killed mid-step" apart from "still loading".
            # We anchor it to whichever agent posted the last event so
            # the sequence diagram renders one final ⚠ on the right
            # actor. Best-effort: if the run had zero events (started_at
            # set but never wrote a span) we skip the insert — there's
            # no agent_id / step to anchor to.
            try:
                last = conn.execute(
                    """
                    SELECT agent_id, agent_name, step
                      FROM events
                     WHERE run_id=?
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if last:
                    agent_id, agent_name, step = last
                    conn.execute(
                        """
                        INSERT INTO events
                          (run_id, agent_id, agent_name, timestamp,
                           event_type, step, data_json)
                        VALUES (?, ?, ?, ?, 'agent_orphaned', ?, ?)
                        """,
                        (
                            run_id, agent_id, agent_name,
                            now.isoformat(), int(step or 0),
                            '{"reason":"sweeper: no activity for '
                            f'{idle_grace_seconds}s; agent process likely '
                            'died (SIGKILL / OOM / host reboot)"}',
                        ),
                    )
            except sqlite3.OperationalError:
                # events table missing or column mismatch on an old DB —
                # the row update is still useful even without the marker.
                pass
            closed += 1
        conn.commit()
        return closed
    finally:
        conn.close()


class OrphanRunSweeper:
    """Background asyncio task that closes orphaned `runs` rows.

    Lifecycle mirrors DiscoverySupervisor: `.start()` after the
    asyncio loop is up; `.stop()` from the shutdown hook. Errors
    during a tick are logged, never crash the loop.
    """

    def __init__(
        self,
        db_path: str,
        *,
        interval_seconds: int = 60,
        max_run_age_seconds: int = 3600,   # 1h — real runs finish in << this
        idle_grace_seconds: int = 600,     # 10m — let slow LLM calls finish
    ):
        self.db_path = db_path
        self.interval = max(15, int(interval_seconds))
        self.max_run_age_seconds = int(max_run_age_seconds)
        self.idle_grace_seconds = int(idle_grace_seconds)
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                closed = await asyncio.to_thread(
                    _sweep_once,
                    self.db_path,
                    max_run_age_seconds=self.max_run_age_seconds,
                    idle_grace_seconds=self.idle_grace_seconds,
                )
                if closed:
                    log.info("orphan-sweeper: force-failed %d stale run(s)", closed)
            except Exception:
                log.exception("orphan-sweeper tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


def sweep_now(
    db_path: str,
    *,
    max_run_age_seconds: int = 3600,
    idle_grace_seconds: int = 600,
) -> int:
    """One-shot helper for ops scripts / tests — runs a single sweep
    synchronously and returns the row count closed."""
    return _sweep_once(
        db_path,
        max_run_age_seconds=max_run_age_seconds,
        idle_grace_seconds=idle_grace_seconds,
    )
