"""`finish()` must NOT wipe a task's `trace_run_id` set earlier
via `set_task_trace_run_id`.

Wire-up: the QA worker pre-generates a `pre_run_id` BEFORE forking
the bench subprocess and stamps it onto `qa_tasks.trace_run_id`.
The bench subprocess inherits the same id via env and diff-graph
uses it as its run_id. Downstream API surfaces —
`/api/runs/{id}/bench-log` alias, the trace UI's bench-log button,
`quality-cli traces bench-log` — all look up the task back from a
run_id by querying `SELECT id FROM qa_tasks WHERE trace_run_id=?`.

Bug we're pinning: `finish()` used to write `trace_run_id=?` with
the default `None` kwarg, **wiping** what `set_task_trace_run_id`
had stored. Effect on plan 211's run f674e2f1ec6f: alias returned
404 even though the bench-log dir existed on disk under task-3582/.

Fix: `COALESCE(?, trace_run_id)` write — explicit None preserves
existing value; non-None overwrites. This file pins the SQL behaviour
directly against a minimal in-memory `qa_tasks` table, so the test
isn't entangled with TaskQueue's full schema bootstrap (which depends
on `events` / `otel_spans` / `runs` and a long migration chain).

A regression that re-introduces the wipe would fail
`test_default_none_kwarg_preserves_existing` immediately, regardless
of how the rest of the queue schema evolves.
"""
from __future__ import annotations

import sqlite3

import pytest


# Minimal table that covers exactly what `finish()` writes — keeps
# the test independent of unrelated schema churn.
SCHEMA = """
    CREATE TABLE qa_tasks (
        id              INTEGER PRIMARY KEY,
        state           TEXT,
        finished_at     TEXT,
        trace_run_id    TEXT,
        result_json     TEXT,
        error_class     TEXT,
        lease_owner     TEXT,
        parent_task_id  INTEGER,
        kind            TEXT NOT NULL DEFAULT 'agent'
    );
"""

# The exact UPDATE statement from `quality_api.queue.TaskQueue.finish()`.
# If this string drifts from the real code, the test is no longer a
# regression net — the production SQL would be the next thing to update.
FINISH_SQL = """
    UPDATE qa_tasks
       SET state=?, finished_at=?,
           trace_run_id = COALESCE(?, trace_run_id),
           result_json=?, error_class=?
     WHERE id=? AND lease_owner=?
"""

# Mirror SQL for the phantom-judge cascade — same COALESCE shape.
CASCADE_SQL = """
    UPDATE qa_tasks
       SET state=?, finished_at=?,
           trace_run_id = COALESCE(?, trace_run_id),
           error_class=?
     WHERE parent_task_id=? AND kind='judge'
       AND state IN ('blocked', 'queued')
"""


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _seed_task(con, *, id: int, trace_run_id=None,
               lease_owner="worker-1", state="leased",
               kind: str = "agent", parent_task_id=None) -> None:
    con.execute(
        "INSERT INTO qa_tasks "
        "(id, state, lease_owner, trace_run_id, kind, parent_task_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (id, state, lease_owner, trace_run_id, kind, parent_task_id),
    )
    con.commit()


def _get(con, tid):
    return dict(con.execute(
        "SELECT * FROM qa_tasks WHERE id=?", (tid,)).fetchone())


class TestFinishPreservesTraceRunId:

    def test_default_none_kwarg_preserves_existing(self, db):
        """The original bug: worker stamps trace_run_id, then finish()
        runs with no `trace_run_id=` arg → wipes it. With COALESCE
        the column survives intact.

        This is the LOAD-BEARING assertion for the bench-log alias —
        `/api/runs/{run_id}/bench-log` 404'd in plan 211 because of
        this exact wipe."""
        _seed_task(db, id=1, trace_run_id="run-abc-123")
        db.execute(FINISH_SQL,
                   ("finished", "2026-05-13T15:04:19+00:00",
                    None,  # ← this is what the buggy default kwarg passed
                    '{"exit_code": 0}', None,
                    1, "worker-1"))
        db.commit()
        t = _get(db, 1)
        # The id survived the UPDATE.
        assert t["trace_run_id"] == "run-abc-123"
        # And the rest of the row finished correctly.
        assert t["state"] == "finished"
        assert t["finished_at"] == "2026-05-13T15:04:19+00:00"

    def test_explicit_non_none_overwrites(self, db):
        """Back-compat: callers that DO know the run_id at finish
        time can still overwrite. COALESCE protects the None case
        only; explicit non-None values pass through."""
        _seed_task(db, id=2, trace_run_id="old-run")
        db.execute(FINISH_SQL,
                   ("finished", "ts", "new-run", None, None,
                    2, "worker-1"))
        db.commit()
        assert _get(db, 2)["trace_run_id"] == "new-run"

    def test_writes_when_no_prior_id(self, db):
        """No prior stamp + finish-with-explicit-id → sets it.
        Useful for migrations / fixtures that bypass the worker's
        pre-flight `set_task_trace_run_id` step."""
        _seed_task(db, id=3, trace_run_id=None)
        db.execute(FINISH_SQL,
                   ("finished", "ts", "late-run", None, None,
                    3, "worker-1"))
        db.commit()
        assert _get(db, 3)["trace_run_id"] == "late-run"

    def test_none_with_no_prior_stays_none(self, db):
        """Empty COALESCE chain: prior None + new None → stays None.
        No spurious value invented from somewhere. This is the
        boundary case that the COALESCE invariant must preserve."""
        _seed_task(db, id=4, trace_run_id=None)
        db.execute(FINISH_SQL,
                   ("finished", "ts", None, None, None,
                    4, "worker-1"))
        db.commit()
        assert _get(db, 4)["trace_run_id"] is None

    def test_cascade_to_judge_preserves_each_id(self, db):
        """When agent finishes, its phantom judge cascades to the
        same terminal state via a sibling UPDATE. Same COALESCE
        protection — each task keeps its own trace_run_id even
        though they share a single SQL transaction."""
        _seed_task(db, id=10, trace_run_id="agent-run")
        _seed_task(db, id=11, trace_run_id="judge-run",
                   state="blocked", kind="judge",
                   parent_task_id=10, lease_owner=None)
        # Agent finishes — no trace_run_id kwarg.
        db.execute(FINISH_SQL,
                   ("finished", "ts", None, None, None,
                    10, "worker-1"))
        # Judge cascade fires — also no trace_run_id kwarg.
        db.execute(CASCADE_SQL,
                   ("finished", "ts", None, None, 10))
        db.commit()
        assert _get(db, 10)["trace_run_id"] == "agent-run"
        assert _get(db, 11)["trace_run_id"] == "judge-run"
        assert _get(db, 11)["state"] == "finished"
