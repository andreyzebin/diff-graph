"""Per-task system log — every subsystem running under a bench task
routes its `logging.*` calls through here so the trace UI / CLI can
show ONE unified, timestamp-ordered, correlation-ID-tagged log.

How it fits together:

  Worker (quality_cli)  ─┐
  Bench subprocess      ─┤
  diff-graph cli.py     ─┼─► system.log  (JSON lines, all sources interleaved by ts)
  Judge                 ─┤
  Orchestra agent loop  ─┘

Each entry-point calls `setup_bench_logging()` at startup. The
function is **idempotent** and reads correlation IDs from env
(`DIFFGRAPH_TASK_ID`, `DIFFGRAPH_TRACE_RUN_ID`, …) so callers don't
need to plumb them through every function. If no `DIFFGRAPH_TASK_ID`
is set the handler is a no-op (test runs, ad-hoc CLI use, etc.) —
no surprise log files.

Log layout:
  `~/.diffgraph/bench-logs/task-{task_id}/system.log`

Sibling files in the same dir:
  `stdout.log` / `stderr.log` — raw subprocess streams written by the
                                worker (file descriptors handed
                                directly to `subprocess.run`).
                                Catches non-Python output (shell,
                                git, anything echo'd) that wouldn't
                                go through `logging`.
  `meta.json`               — correlation IDs, cmd, exit code, etc.

API + CLI consume the dir as a whole; the UI's bench-log tab fetches
`system.log` by default with the option to switch to stdout/stderr.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional


_INSTALL_LOCK = threading.Lock()
# Sentinel attached to the root logger after a successful install so
# repeated calls are cheap no-ops instead of producing N stacked
# handlers. Each call refreshes the cached IDs and the level.
_INSTALL_MARKER = "_bench_log_handler"


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per log record. The shape is stable so
    downstream tooling (API, UI, CLI) can rely on the same keys:

      ts        — ISO-8601 UTC with millisecond precision
      level     — DEBUG / INFO / WARNING / ERROR / CRITICAL
      logger    — Python logger name (e.g. `orchestra.agent`)
      system    — coarse subsystem hint: `worker` / `bench` /
                  `diffgraph` / `judge` / `unknown`. Comes from the
                  `BENCH_LOG_SYSTEM` env var, set by each entry-
                  point; gives the UI / human reader an at-a-glance
                  "who emitted this".
      msg       — formatted message text (`%`-args already applied)
      task_id, run_id, plan_id, scenario_id  — correlation keys
      exc       — exception summary when `exc_info` was attached
                  (one-line, full traceback goes in `traceback`).
      traceback — multi-line traceback for ERROR / CRITICAL records
                  with an exception, otherwise absent.
    """

    def __init__(self, *, ids: dict, system: str):
        super().__init__()
        self._ids = dict(ids)
        self._system = system

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(record.created),
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "system": self._system,
            "msg": record.getMessage(),
        }
        # Correlation keys — surface every non-empty one so cross-
        # system queries (e.g. "give me all log lines for plan 192")
        # can fan out without re-joining via task_id.
        for k, v in self._ids.items():
            if v:
                payload[k] = v
        if record.exc_info:
            etype, evalue, _tb = record.exc_info
            payload["exc"] = f"{etype.__name__}: {evalue}"
            payload["traceback"] = self.formatException(record.exc_info)
        # `extra={...}` kwargs from caller — surface as top-level
        # keys so they're searchable. Caller is expected to use
        # snake_case namespacing (e.g. `cmd`, `exit_code`) to avoid
        # colliding with our fixed schema.
        for k, v in record.__dict__.items():
            if k in ("args", "msg", "exc_info", "exc_text", "stack_info",
                     "name", "levelno", "levelname", "created",
                     "msecs", "relativeCreated", "pathname", "filename",
                     "module", "lineno", "funcName", "process",
                     "processName", "thread", "threadName"):
                continue
            if k in payload:
                continue
            try:
                json.dumps(v)  # serialisable?
                payload[k] = v
            except Exception:
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def _log_dir_for(task_id: str) -> Path:
    """Resolve the per-task log directory. Falls back to a worker-
    process tempdir when task_id is unset so the handler can still
    install without writing anywhere meaningful — useful for tests
    and ad-hoc CLI invocations."""
    base = Path(os.environ.get("DIFFGRAPH_BENCH_LOGS_DIR") or
                Path.home() / ".diffgraph" / "bench-logs")
    return base / f"task-{task_id or 'unbound'}"


def setup_bench_logging(
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    scenario_id: Optional[str] = None,
    system: Optional[str] = None,
    level: int = logging.INFO,
) -> Optional[Path]:
    """Attach a JSON-line file handler to the root logger that
    writes to the current task's `system.log`.

    Returns the log file path on success, or `None` when there's no
    `task_id` in scope (env var unset + no kwarg) — in that case the
    handler is NOT attached so the function is a safe call from any
    entry-point.

    Idempotent across repeated calls in the same process: if the
    handler was already installed, the cached IDs are refreshed and
    a fresh log line tagged `system="<system>"` records the
    re-entry. Per-process global because that's how Python's logging
    framework hangs handlers.

    Caller is expected to pass `system=` so log lines can be visually
    attributed to the right subsystem (`worker` / `bench` /
    `diffgraph` / `judge`). If omitted, we read `BENCH_LOG_SYSTEM`
    from env; default `unknown`.
    """
    task_id = task_id or os.environ.get("DIFFGRAPH_TASK_ID", "")
    run_id = run_id or os.environ.get("DIFFGRAPH_TRACE_RUN_ID", "")
    plan_id = plan_id or os.environ.get("DIFFGRAPH_PLAN_ID", "")
    scenario_id = scenario_id or os.environ.get("DIFFGRAPH_SCENARIO_ID", "")
    system = system or os.environ.get("BENCH_LOG_SYSTEM", "unknown")
    if not task_id:
        return None  # no task scope → no log file

    log_dir = _log_dir_for(task_id)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    log_path = log_dir / "system.log"

    ids = {
        "task_id": task_id,
        "run_id": run_id,
        "plan_id": plan_id,
        "scenario_id": scenario_id,
    }

    with _INSTALL_LOCK:
        root = logging.getLogger()
        # Replace any previous bench handler so the IDs stay current
        # — e.g. when a cli.py subprocess re-runs setup with a more
        # specific run_id than the parent worker had.
        for h in list(root.handlers):
            if getattr(h, _INSTALL_MARKER, False):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(_JsonLineFormatter(ids=ids, system=system))
        handler.setLevel(level)
        setattr(handler, _INSTALL_MARKER, True)
        root.addHandler(handler)
        # Root level may have been set higher than our target —
        # don't lower it across the process, but make sure our
        # records pass through.
        if root.level > level or root.level == logging.NOTSET:
            root.setLevel(level)

    # First line after install — gives the reader a header showing
    # who joined the log and when. Also doubles as a smoke test that
    # the handler is wired correctly (caller doesn't have to write
    # any explicit log line to see the file exist).
    logging.getLogger("orchestra.bench_log").info(
        "bench_log handler installed", extra={
            "event": "setup",
            "pid": os.getpid(),
            "argv0": sys.argv[0] if sys.argv else "",
        },
    )
    return log_path


def current_log_path() -> Optional[Path]:
    """Where this process is currently writing (or `None` if not
    installed). Lets callers print the path to the operator at
    startup so they know where to look."""
    task_id = os.environ.get("DIFFGRAPH_TASK_ID", "")
    if not task_id:
        return None
    p = _log_dir_for(task_id) / "system.log"
    return p if p.exists() else None
