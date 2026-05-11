"""
Reactive sweeper for `runs` rows that stayed `status='running'`
because the cli.py process was SIGKILL'd / OOM-killed / host-rebooted
before the new try/finally finalisation could fire.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tracing.server.orphan_sweeper import sweep_now


def _now_iso(delta_seconds: int = 0) -> str:
    """Now ± delta as an ISO string with TZ — matches what trace_db writes."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_seconds)
    return t.isoformat()


def _now_ns(delta_seconds: int = 0) -> int:
    """Now ± delta in nanoseconds — matches otel_spans.start_ns."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_seconds)
    return int(t.timestamp() * 1e9)


@pytest.fixture
def fresh_db(tmp_path):
    """Minimal `runs` + `otel_spans` schema — just what the sweeper reads."""
    db_path = tmp_path / "sweep.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT
        );
        CREATE TABLE otel_spans (
            span_id TEXT,
            start_ns INTEGER,
            end_ns INTEGER,
            attributes TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def _insert_run(db_path: str, *, run_id: str, started_at: str,
                status: str = "running", finished_at: str | None = None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO runs(id, started_at, finished_at, status) VALUES(?,?,?,?)",
        (run_id, started_at, finished_at, status),
    )
    conn.commit()
    conn.close()


def _insert_span(db_path: str, *, run_id: str, start_ns: int):
    conn = sqlite3.connect(db_path)
    attrs = f'{{"diffgraph.run_id": "{run_id}"}}'
    conn.execute(
        "INSERT INTO otel_spans(span_id, start_ns, end_ns, attributes) "
        "VALUES(?,?,?,?)",
        ("sp" + run_id[:8], start_ns, start_ns + 1_000_000, attrs),
    )
    conn.commit()
    conn.close()


def _read_run(db_path: str, run_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


class TestOrphanSweep:
    def test_old_quiet_run_force_failed(self, fresh_db):
        """A run that's `running`, started > max_run_age_seconds ago,
        with no recent spans — sweeper marks it failed."""
        _insert_run(fresh_db, run_id="orphan1", started_at=_now_iso(-7200))
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 1
        row = _read_run(fresh_db, "orphan1")
        assert row["status"] == "failed"
        assert row["finished_at"] is not None

    def test_recent_run_left_alone(self, fresh_db):
        """A run that's `running` but barely 5min old — leave it
        alone; below the age threshold."""
        _insert_run(fresh_db, run_id="young", started_at=_now_iso(-300))
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 0
        assert _read_run(fresh_db, "young")["status"] == "running"

    def test_old_but_still_active_run_left_alone(self, fresh_db):
        """Run is old (> max_run_age_seconds) BUT has a span within
        the idle window — could be a long-running LLM call,
        don't kill it."""
        _insert_run(fresh_db, run_id="slow", started_at=_now_iso(-7200))
        _insert_span(fresh_db, run_id="slow", start_ns=_now_ns(-60))  # 1m ago
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 0
        assert _read_run(fresh_db, "slow")["status"] == "running"

    def test_old_with_only_stale_spans_force_failed(self, fresh_db):
        """Run is old, has a span — but the span is BEFORE the idle
        window. Quiet for long enough → force-fail."""
        _insert_run(fresh_db, run_id="stalled", started_at=_now_iso(-7200))
        _insert_span(fresh_db, run_id="stalled", start_ns=_now_ns(-3600))  # 1h ago
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 1
        assert _read_run(fresh_db, "stalled")["status"] == "failed"

    def test_completed_runs_untouched(self, fresh_db):
        """Status != 'running' is out of scope, regardless of age."""
        _insert_run(fresh_db, run_id="done", started_at=_now_iso(-7200),
                    status="completed", finished_at=_now_iso(-7000))
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 0
        assert _read_run(fresh_db, "done")["status"] == "completed"

    def test_finished_at_preserved_when_already_set(self, fresh_db):
        """COALESCE-style: if finished_at is somehow already populated
        on a status='running' row, the sweeper keeps the existing
        value rather than clobbering it."""
        old_finish = _now_iso(-100)
        _insert_run(fresh_db, run_id="weird", started_at=_now_iso(-7200),
                    status="running", finished_at=old_finish)
        sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert _read_run(fresh_db, "weird")["finished_at"] == old_finish

    def test_started_at_null_skipped(self, fresh_db):
        """Defensive: rows with NULL started_at can't be aged in or
        out — leave them for human investigation."""
        _insert_run(fresh_db, run_id="weird2", started_at=None)
        closed = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert closed == 0
        assert _read_run(fresh_db, "weird2")["status"] == "running"

    def test_idempotent(self, fresh_db):
        """Running sweep twice on the same DB produces no double-close."""
        _insert_run(fresh_db, run_id="o1", started_at=_now_iso(-7200))
        first = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        second = sweep_now(fresh_db, max_run_age_seconds=3600, idle_grace_seconds=600)
        assert first == 1
        assert second == 0
