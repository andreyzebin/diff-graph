"""Per-task system log — `orchestra.bench_log.setup_bench_logging`.

When a Python process is spawned under a QA worker task
(`DIFFGRAPH_TASK_ID` env set), every `logging.*` call in that process
should land as a JSON line in
`~/.diffgraph/bench-logs/task-{task_id}/system.log`. Multiple
subsystems (worker, bench, diff-graph cli, judge) all opt into the
same handler so a single file holds a unified timestamp-ordered
view per task.

These tests pin the handler contract:
  - No-op without a task_id (ad-hoc / test runs don't leak logs)
  - File created at the right path under `DIFFGRAPH_BENCH_LOGS_DIR`
  - Each line is a parseable JSON object with the documented fields
  - Correlation IDs from env are surfaced on every line
  - Idempotent across repeated calls; second call replaces the first
    handler (so the IDs stay current after a subprocess re-installs)
  - Exception logging captures traceback as a separate field
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from orchestra.bench_log import (
    setup_bench_logging,
    current_log_path,
)


@pytest.fixture(autouse=True)
def _isolated_handler(tmp_path, monkeypatch):
    """Each test gets its own bench-logs root + a clean root logger.

    Without the cleanup pass between tests, a handler attached in one
    test would keep writing to its log file from any subsequent test
    using `logging.*` calls. We strip every bench-log handler at the
    end of each test by checking the install marker the handler sets.
    """
    monkeypatch.setenv("DIFFGRAPH_BENCH_LOGS_DIR", str(tmp_path))
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_bench_log_handler", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


class TestSetupBenchLogging:

    def test_no_task_id_returns_none_and_writes_nothing(self, tmp_path, monkeypatch):
        """Without DIFFGRAPH_TASK_ID and no explicit kwarg, the helper
        must be a quiet no-op. We don't want test runs / ad-hoc CLI
        invocations creating random log directories under the user's
        home."""
        monkeypatch.delenv("DIFFGRAPH_TASK_ID", raising=False)
        result = setup_bench_logging(system="test")
        assert result is None
        # And the log dir wasn't even created.
        assert list(tmp_path.iterdir()) == []

    def test_explicit_task_id_creates_log_file(self, tmp_path):
        """Caller passes task_id explicitly (rather than via env) →
        handler installs, file is created, the install-banner line
        lands as the first record."""
        log_path = setup_bench_logging(task_id="42", system="worker")
        assert log_path is not None
        assert log_path == tmp_path / "task-42" / "system.log"
        assert log_path.exists()
        # First line is the setup banner — confirms handler is wired.
        first = log_path.read_text().strip().splitlines()[0]
        rec = json.loads(first)
        assert rec["msg"] == "bench_log handler installed"
        assert rec["system"] == "worker"
        assert rec["task_id"] == "42"

    def test_log_calls_land_as_json_lines(self, tmp_path):
        """Real `logging.*` calls go through the installed handler
        and produce one JSON object per line."""
        setup_bench_logging(task_id="7", system="bench")
        log = logging.getLogger("benchmark.runner.run")
        log.info("starting scenario %s", "SCEN-009")
        log.warning("git push retry %d", 2)

        path = tmp_path / "task-7" / "system.log"
        lines = path.read_text().strip().splitlines()
        # banner + 2 user lines
        assert len(lines) == 3
        recs = [json.loads(l) for l in lines]
        # The info line is the second record.
        assert recs[1]["level"] == "INFO"
        assert recs[1]["logger"] == "benchmark.runner.run"
        assert recs[1]["msg"] == "starting scenario SCEN-009"
        assert recs[1]["task_id"] == "7"
        # The warning is the third.
        assert recs[2]["level"] == "WARNING"
        assert recs[2]["msg"] == "git push retry 2"

    def test_correlation_ids_from_env_surface_on_every_line(self, tmp_path, monkeypatch):
        """All four documented correlation keys (task / run / plan /
        scenario) come from env and appear on every JSON line —
        downstream tooling can grep by any one of them without doing
        a join."""
        monkeypatch.setenv("DIFFGRAPH_TASK_ID", "100")
        monkeypatch.setenv("DIFFGRAPH_TRACE_RUN_ID", "abcd1234")
        monkeypatch.setenv("DIFFGRAPH_PLAN_ID", "200")
        monkeypatch.setenv("DIFFGRAPH_SCENARIO_ID", "SCEN-X")
        setup_bench_logging(system="diffgraph")
        logging.getLogger("orchestra.agent").info("agent.start")

        path = tmp_path / "task-100" / "system.log"
        recs = [json.loads(l) for l in path.read_text().strip().splitlines()]
        # Pick any non-banner record; correlation keys must all be there.
        rec = recs[-1]
        assert rec["task_id"] == "100"
        assert rec["run_id"] == "abcd1234"
        assert rec["plan_id"] == "200"
        assert rec["scenario_id"] == "SCEN-X"
        assert rec["system"] == "diffgraph"

    def test_exception_capture_has_traceback_field(self, tmp_path):
        """`logger.exception(...)` or `exc_info=True` records produce
        a separate `traceback` field — useful for the UI when it
        wants to highlight only error lines + their stack."""
        setup_bench_logging(task_id="8", system="bench")
        log = logging.getLogger("test.errors")
        try:
            raise ValueError("synthetic failure")
        except ValueError:
            log.exception("failed to clone branch")

        path = tmp_path / "task-8" / "system.log"
        recs = [json.loads(l) for l in path.read_text().strip().splitlines()]
        err_rec = [r for r in recs if r.get("level") == "ERROR"][0]
        assert err_rec["msg"] == "failed to clone branch"
        assert err_rec["exc"].startswith("ValueError: synthetic failure")
        assert "Traceback" in err_rec["traceback"]
        assert "synthetic failure" in err_rec["traceback"]

    def test_extra_kwargs_surface_as_top_level_fields(self, tmp_path):
        """`logger.info("...", extra={"cmd": "..."})` should put `cmd`
        into the JSON line as a top-level key, so the UI can render
        structured columns without re-parsing the message text."""
        setup_bench_logging(task_id="9", system="worker")
        log = logging.getLogger("test.extras")
        log.info("subprocess returned", extra={
            "cmd": "bench run-integration SCEN-009",
            "exit_code": 0,
        })
        path = tmp_path / "task-9" / "system.log"
        rec = json.loads(path.read_text().strip().splitlines()[-1])
        assert rec["cmd"] == "bench run-integration SCEN-009"
        assert rec["exit_code"] == 0

    def test_idempotent_repeated_install_refreshes_ids(self, tmp_path, monkeypatch):
        """A subprocess that re-installs the handler with a more
        specific run_id (the worker only had the task_id baseline)
        should see the NEW run_id on subsequent lines — old handler
        is removed, new one with updated IDs is installed."""
        # First install — no run_id yet
        monkeypatch.delenv("DIFFGRAPH_TRACE_RUN_ID", raising=False)
        setup_bench_logging(task_id="50", system="worker")
        logging.getLogger("test").info("worker line")
        # Re-install with run_id (simulates subprocess setup)
        setup_bench_logging(task_id="50", run_id="newrun", system="bench")
        logging.getLogger("test").info("bench line")

        path = tmp_path / "task-50" / "system.log"
        recs = [json.loads(l) for l in path.read_text().strip().splitlines()]
        worker_line = [r for r in recs if r.get("msg") == "worker line"][0]
        bench_line = [r for r in recs if r.get("msg") == "bench line"][0]
        # First half tagged worker, no run_id (env var was absent).
        assert worker_line["system"] == "worker"
        assert "run_id" not in worker_line or not worker_line.get("run_id")
        # Second half tagged bench, run_id now present.
        assert bench_line["system"] == "bench"
        assert bench_line["run_id"] == "newrun"
        # Only ONE bench_log handler installed at any time — the
        # re-install removed the prior one. Sanity check.
        root = logging.getLogger()
        bench_handlers = [h for h in root.handlers
                           if getattr(h, "_bench_log_handler", False)]
        assert len(bench_handlers) == 1

    def test_current_log_path_returns_active_file(self, tmp_path, monkeypatch):
        """The helper returns the file path callers can show to the
        operator at startup ("logs at ..."). When the handler isn't
        installed, returns None — caller's prompt stays uncluttered
        instead of printing a non-existent path."""
        monkeypatch.delenv("DIFFGRAPH_TASK_ID", raising=False)
        assert current_log_path() is None
        # After install + env var, both task-id sources should work.
        monkeypatch.setenv("DIFFGRAPH_TASK_ID", "11")
        setup_bench_logging(task_id="11", system="test")
        assert current_log_path() == tmp_path / "task-11" / "system.log"
