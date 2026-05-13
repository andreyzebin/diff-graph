"""API contract for `/api/qa/tasks/{id}/bench-log` and the run-id
alias.

These endpoints surface the per-task bench-log directory produced
by the QA worker — stdout/stderr from the bench subprocess plus
the unified Python `logging` stream (`system.log`) and a `meta.json`
header. The contract:

  - GET .../bench-log              → text combined view (META +
                                       SYSTEM + STDOUT + STDERR,
                                       header bars)
  - GET .../bench-log?as=json      → envelope {data: {meta, stdout,
                                       stderr, system}}
  - GET .../bench-log?stream=stderr → just stderr stream
  - 404 in text mode → PlainTextResponse so CLI piping doesn't get
                       JSON-wrapped error envelopes
  - Run-id alias resolves via `qa_tasks.trace_run_id`; 404 when no
    task carries the run_id (anonymous / pre-trace-run-id era runs)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def bench_log_root(tmp_path, monkeypatch):
    """Redirect bench-log lookups to a temp dir for the test. Both
    the worker's writer and the API endpoint honour
    `DIFFGRAPH_BENCH_LOGS_DIR`, so a single env var pins the whole
    flow without touching the user's `~/.diffgraph`."""
    monkeypatch.setenv("DIFFGRAPH_BENCH_LOGS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def db_with_task(monkeypatch):
    """Tiny SQLite with one qa_tasks row whose trace_run_id matches
    our test run_id. The run-id alias endpoint joins through this
    table — without it the test for the alias 404 wouldn't be
    distinguishable from a generic missing-task 404."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    con = sqlite3.connect(str(db_path))
    con.execute("""
      CREATE TABLE qa_tasks (
        id INTEGER PRIMARY KEY,
        trace_run_id TEXT
      )
    """)
    con.execute("INSERT INTO qa_tasks (id, trace_run_id) VALUES (?, ?)",
                (777, "runabc123def"))
    con.commit()
    con.close()
    import tracing.server.app as app_mod
    monkeypatch.setattr(app_mod, "DEFAULT_DB_PATH", db_path)
    yield db_path
    db_path.unlink(missing_ok=True)


def _seed_log_dir(root: Path, task_id: int, *,
                   stdout="", stderr="", system="", meta=None):
    """Write the four files the worker would have produced. The
    endpoint reads them as-is."""
    d = root / f"task-{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        d.joinpath("meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False))
    if stdout:
        d.joinpath("stdout.log").write_text(stdout)
    if stderr:
        d.joinpath("stderr.log").write_text(stderr)
    if system:
        d.joinpath("system.log").write_text(system)


@pytest.fixture
def client():
    from tracing.server.app import app
    return TestClient(app)


# ── task-keyed endpoint ────────────────────────────────────────────


class TestTaskBenchLog:

    def test_combined_text_view(self, bench_log_root, client):
        _seed_log_dir(
            bench_log_root, 42,
            meta={"task_id": 42, "scenario_id": "SCEN-009",
                  "exit_code": 1, "cmd": "bench run-integration SCEN-009"},
            stdout="Cloning bench/SCEN-009/abc...\n",
            stderr="fatal: Remote branch not found in upstream\n",
            system='{"ts":"2026-05-13T14:18:55Z","level":"INFO","msg":"start"}\n',
        )
        r = client.get("/api/qa/tasks/42/bench-log")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        # All three sections appear, in the documented order (META
        # → SYSTEM → STDOUT → STDERR). Empty streams would be hidden;
        # here all three have content so all three are present.
        assert "META" in body
        assert "SYSTEM" in body
        assert "STDOUT" in body
        assert "STDERR" in body
        # Section order (regression on the docstring's promise).
        assert body.index("META") < body.index("SYSTEM") < body.index("STDOUT") < body.index("STDERR")
        # Real content survives byte-identical.
        assert "fatal: Remote branch not found in upstream" in body
        assert "Cloning bench/SCEN-009/abc..." in body
        assert "SCEN-009" in body  # from meta JSON

    def test_empty_streams_hidden_from_combined(self, bench_log_root, client):
        """A task that only emitted stderr (e.g. bench died before
        any stdout) should not display "(empty stdout)" — the
        combined view drops the empty sections so the reader's eye
        lands on the actual content."""
        _seed_log_dir(
            bench_log_root, 43,
            meta={"task_id": 43, "exit_code": 1},
            stderr="git: command not found\n",
        )
        r = client.get("/api/qa/tasks/43/bench-log")
        body = r.text
        assert "STDERR" in body
        assert "STDOUT" not in body  # empty → hidden
        assert "SYSTEM" not in body
        assert "git: command not found" in body

    def test_single_stream_selector(self, bench_log_root, client):
        """`?stream=stderr` returns just that file's contents — no
        header bars, no envelope. Used by the CLI to pipe straight
        into other tools."""
        _seed_log_dir(
            bench_log_root, 44,
            stdout="ok\n",
            stderr="err1\nerr2\n",
        )
        r = client.get("/api/qa/tasks/44/bench-log?stream=stderr")
        assert r.status_code == 200
        assert r.text == "err1\nerr2\n"

    def test_as_json_envelope(self, bench_log_root, client):
        """`?as=json` returns each stream as a separate field plus
        the parsed meta. Lets the UI cache both views with a single
        fetch so the { } JSON toggle is local."""
        _seed_log_dir(
            bench_log_root, 45,
            meta={"task_id": 45, "exit_code": 0},
            stdout="hello\n",
            stderr="",
            system='{"level":"INFO"}\n',
        )
        r = client.get("/api/qa/tasks/45/bench-log?as=json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        d = body["data"]
        assert d["meta"]["task_id"] == 45
        assert d["stdout"] == "hello\n"
        assert d["stderr"] == ""
        assert d["system"] == '{"level":"INFO"}\n'

    def test_text_404_for_missing_task(self, bench_log_root, client):
        """No log dir → 404 with PlainTextResponse so a CLI pipe gets
        a one-line message, not a JSON error envelope."""
        r = client.get("/api/qa/tasks/9999/bench-log")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert "no bench-log dir" in r.text or "9999" in r.text

    def test_json_404_for_missing_task(self, bench_log_root, client):
        """`?as=json` 404 returns the standard error envelope so
        programmatic consumers can react."""
        r = client.get("/api/qa/tasks/9998/bench-log?as=json")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json().get("error", {}).get("code") == "not_found"


# ── run-id alias ──────────────────────────────────────────────────


class TestRunBenchLogAlias:

    def test_alias_resolves_via_qa_tasks(self, bench_log_root,
                                          db_with_task, client):
        """`/api/runs/{run_id}/bench-log` looks up the qa_tasks row
        whose `trace_run_id` matches and dispatches to the task-keyed
        endpoint. Seeded fixture: task 777, run runabc123def."""
        _seed_log_dir(
            bench_log_root, 777,
            meta={"task_id": 777, "scenario_id": "SCEN-X"},
            stdout="hello from task 777\n",
        )
        r = client.get("/api/runs/runabc123def/bench-log")
        assert r.status_code == 200
        assert "hello from task 777" in r.text

    def test_alias_404_when_run_has_no_task(self, db_with_task, client):
        """Run_id with no matching trace_run_id in qa_tasks (local /
        anonymous runs, or sessions that predate the field) returns
        404 with a helpful one-liner — distinct from "task exists
        but no log dir"."""
        r = client.get("/api/runs/nosuchrun/bench-log")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert "no associated task" in r.text or "nosuchrun" in r.text
