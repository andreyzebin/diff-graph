"""Periodic SQLite WAL checkpoint for the trace DB.

Symptom this fixes: with bench workers + agent runs constantly
writing into `traces.db`, the WAL grows much faster than the
default `wal_autocheckpoint=1000 pages (~4MB)` setting can drain.
Active read transactions from the tracing UI hold the checkpoint
back ("RESTART" cannot run while any reader has a snapshot older
than the WAL frame it'd reclaim), so the WAL keeps growing.

Observed: 3.2GB main DB + 30MB WAL pre-checkpoint, individual
`/api/search/runs?limit=1` calls taking 5–13s because each reader
had to scan the entire WAL on connect. A manual
`PRAGMA wal_checkpoint(TRUNCATE)` brought the WAL to 0 bytes and
restored sub-millisecond reads.

This task runs `wal_checkpoint(TRUNCATE)` on a schedule, accepting
the occasional `SQLITE_BUSY` (we just try again next tick).
Lifecycle mirrors `OrphanRunSweeper`: `.start()` after the asyncio
loop is up, `.stop()` from the shutdown hook.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)


def checkpoint_now(db_path: str) -> tuple[int, int, int]:
    """Run `PRAGMA wal_checkpoint(TRUNCATE)` and return the
    `(busy, log_frames, checkpointed_frames)` triple SQLite emits.
    `busy=1` means a reader blocked the checkpoint; we'll try again
    next tick. `log_frames == checkpointed_frames` after a successful
    TRUNCATE means the WAL is empty afterwards."""
    con = sqlite3.connect(db_path, timeout=30.0)
    try:
        # PRAGMA wal_checkpoint returns (busy, log_frames, ckpt_frames)
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    if row is None:
        return (0, 0, 0)
    return int(row[0]), int(row[1]), int(row[2])


class WalCheckpointer:
    """Background asyncio task that calls
    `PRAGMA wal_checkpoint(TRUNCATE)` periodically against the trace
    DB. Errors during a tick are logged, never crash the loop.

    Default cadence (`interval_seconds=300` = 5 min) is a compromise
    between latency tax (each checkpoint scans the WAL) and growth
    headroom (5 min of bench traffic is well under the disk we
    have)."""

    def __init__(
        self,
        db_path: str,
        *,
        interval_seconds: int = 300,
    ):
        self.db_path = db_path
        # 30s minimum so a misconfigured tick doesn't pummel the DB.
        self.interval = max(30, int(interval_seconds))
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
                busy, frames, ckpt = await asyncio.to_thread(
                    checkpoint_now, self.db_path,
                )
                # Quiet on the boring path. The interesting cases:
                # - `busy=1` and frames > 0 → reader blocked drain;
                #    the WAL keeps growing until the reader releases.
                # - large frames + matching ckpt → healthy reclaim,
                #    worth a debug-level log so a tail can see it.
                if busy:
                    log.info(
                        "wal-checkpoint: busy (reader held the WAL), "
                        "frames=%d ckpt=%d", frames, ckpt,
                    )
                elif frames > 0:
                    log.debug(
                        "wal-checkpoint: reclaimed %d/%d frames",
                        ckpt, frames,
                    )
            except Exception:
                log.exception("wal-checkpoint tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
