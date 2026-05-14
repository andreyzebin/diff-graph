"""Minimal trace writer for non-agent runs (judge / future evaluators).

Mirrors orchestra's dual-storage scheme so judge calls land in the
same shape as agent calls (TODO §5e.10a):

  Filesystem layout (per run_dir):
      run.json
      events.jsonl
      agents/<sub_agent>/
          meta.json
          step-NN-request.json
          step-NN-response.json
          step-NN-tool-SS-request.json     (judges typically don't have these)
          step-NN-tool-SS-response.json

  SQLite (default ~/.diffgraph/traces.db):
      runs row with kind='judge'
      events rows with the LLM request/response/error events

Lives in the bench because the bench can't import orchestra (separate
venv). The schema and layout track orchestra's TraceFSWriter /
TraceDBWriter exactly so a unified web UI can render both kinds
without special-casing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


DEFAULT_DB_PATH = Path.home() / ".diffgraph" / "traces.db"


class JudgeTraceWriter:
    """Single-shot trace writer for an LLM judge call.

    One instance per judge invocation. Writes a `runs` row at
    construction (kind='judge'), plus per-step request/response files
    + events. Multi-step judges (future evaluators) just call write_step
    multiple times.

    All writes are best-effort — failures must not crash the bench.
    """

    def __init__(self, run_dir: str | Path | None,
                 db_path: str | Path = DEFAULT_DB_PATH,
                 model: str = "",
                 sub_agent_name: str = "judge",
                 kind: str = "judge",
                 scenario_id: str = "",
                 scenario_tags: list[str] | None = None,
                 linked_run_id: str = ""):
        self.kind = kind
        self.model = model
        self.sub_agent = sub_agent_name
        self.run_id = str(uuid.uuid4())[:12]
        self.started_at = datetime.now().isoformat()
        # Search-dimension metadata (TODO §5e.11) — denormalised
        # onto runs row at insert.
        self.scenario_id = scenario_id or ""
        self.scenario_tags = list(scenario_tags or [])
        self.linked_run_id = linked_run_id or ""
        # fs_trace_path is the run_dir if FS half is enabled; populated
        # below once we've decided whether the FS half is actually active.
        self._fs_trace_path = ""

        # Filesystem half (optional)
        self.run_dir: Optional[Path] = None
        self._events_fp = None
        self._sub_dir: Optional[Path] = None
        if run_dir is not None:
            try:
                self.run_dir = Path(run_dir).expanduser()
                self.run_dir.mkdir(parents=True, exist_ok=True)
                self._sub_dir = self.run_dir / "agents" / sub_agent_name
                self._sub_dir.mkdir(parents=True, exist_ok=True)
                self._events_fp = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")
                self._fs_trace_path = str(self.run_dir)
                self._write_run_json({
                    "run_id": self.run_id,
                    "kind": kind,
                    "started_at": self.started_at,
                    "model": model,
                    "scenario_id": self.scenario_id,
                    "scenario_tags": self.scenario_tags,
                    "linked_run_id": self.linked_run_id,
                    "status": "running",
                })
                self._write_agent_meta({
                    "agent_id": self.run_id,
                    "agent_name": sub_agent_name,
                    "parent_id": "",
                    "depth": 0,
                    "started_at": self.started_at,
                    "status": "running",
                })
            except Exception:
                # FS half is best-effort; carry on with SQLite-only.
                self.run_dir = None
                self._events_fp = None
                self._sub_dir = None
                self._fs_trace_path = ""

        # SQLite half (always attempted)
        self._db_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            db_path = Path(db_path).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema()
            self._insert_run()
        except Exception:
            self._conn = None

    # ── Public API ────────────────────────────────────────────────────────

    def write_step(self, step: int,
                   request: dict[str, Any],
                   response: dict[str, Any] | None = None,
                   error: str | None = None) -> None:
        """Write a single step's request + response/error.

        For single-shot judges, call once with step=0.
        """
        ts_req = datetime.now().isoformat()
        # Filesystem
        self._write_step_file(f"step-{step:02d}-request.json", {
            "step": step, "ts": ts_req,
            "model": self.model,
            **request,
        })
        # SQLite event
        self._emit_event("agent_llm_request", step=step, **request)

        if error is not None:
            self._write_step_file(f"step-{step:02d}-error.json", {
                "step": step, "ts": datetime.now().isoformat(),
                "error": error,
            })
            self._emit_event("agent_error", step=step, error=error)
            return

        if response is not None:
            self._write_step_file(f"step-{step:02d}-response.json", {
                "step": step, "ts": datetime.now().isoformat(),
                **response,
            })
            self._emit_event("agent_llm_response", step=step, **response)

    def finish(self, status: str = "completed",
               extra: dict[str, Any] | None = None) -> None:
        finished_at = datetime.now().isoformat()
        # duration_ms (denormalised for sort)
        duration_ms = None
        try:
            started = datetime.fromisoformat(self.started_at)
            duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        except Exception:
            pass
        # FS run.json
        if self.run_dir is not None:
            try:
                payload = self._read_run_json()
                payload.update({
                    "finished_at": finished_at,
                    "status": status,
                })
                if extra:
                    payload.update(extra)
                self._write_run_json(payload)
                # Agent meta
                meta = {
                    "agent_id": self.run_id,
                    "agent_name": self.sub_agent,
                    "started_at": self.started_at,
                    "finished_at": finished_at,
                    "status": "done" if status == "completed" else status,
                }
                self._write_agent_meta(meta)
            except Exception:
                pass
            try:
                if self._events_fp:
                    self._events_fp.close()
                    self._events_fp = None
            except Exception:
                pass
        # SQLite
        if self._conn is not None:
            try:
                with self._db_lock:
                    self._conn.execute(
                        "UPDATE runs SET finished_at=?, status=?, model=?, "
                        "duration_ms=? WHERE id=?",
                        (finished_at, status, self.model, duration_ms, self.run_id),
                    )
                    self._conn.commit()
                    self._conn.close()
                self._conn = None
            except Exception:
                pass

    # ── Filesystem helpers ────────────────────────────────────────────────

    def _write_run_json(self, data: dict) -> None:
        if self.run_dir is None:
            return
        path = self.run_dir / "run.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(path)
        except Exception:
            pass

    def _read_run_json(self) -> dict:
        if self.run_dir is None:
            return {}
        path = self.run_dir / "run.json"
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_agent_meta(self, meta: dict) -> None:
        if self._sub_dir is None:
            return
        path = self._sub_dir / "meta.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

    def _write_step_file(self, name: str, payload: dict) -> None:
        if self._sub_dir is None:
            return
        path = self._sub_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(path)
        except Exception:
            pass
        # also append to events.jsonl
        if self._events_fp is not None:
            try:
                self._events_fp.write(json.dumps({
                    "ts": datetime.now().isoformat(),
                    "kind": self.kind,
                    "file": name,
                    **{k: v for k, v in payload.items() if k != "ts"},
                }, ensure_ascii=False, default=str) + "\n")
                self._events_fp.flush()
            except Exception:
                pass

    # ── SQLite helpers ────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        # Same schema as orchestra/trace_db.py — kind column added
        # idempotently. We rely on `IF NOT EXISTS` and ALTER guards.
        if self._conn is None:
            return
        with self._db_lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT,
                    finished_at TEXT,
                    model TEXT,
                    pr_url TEXT,
                    diff_summary TEXT,
                    total_tokens_paid INTEGER,
                    findings_count INTEGER,
                    status TEXT DEFAULT 'running',
                    prompt_source TEXT,
                    prompt_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    agent_id TEXT,
                    agent_name TEXT,
                    timestamp TEXT,
                    event_type TEXT,
                    step INTEGER,
                    data_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
                CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
            """)
            for col, decl in [
                ("prompt_source", "TEXT"),
                ("prompt_hash", "TEXT"),
                ("tags", "TEXT"),
                ("kind", "TEXT DEFAULT 'agent'"),
            ]:
                try:
                    self._conn.execute(f"SELECT {col} FROM runs LIMIT 0")
                except sqlite3.OperationalError:
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind)")
            self._conn.commit()

    def _insert_run(self) -> None:
        if self._conn is None:
            return
        try:
            tags_json = (
                json.dumps(self.scenario_tags, ensure_ascii=False)
                if self.scenario_tags else None
            )
            with self._db_lock:
                self._conn.execute(
                    "INSERT INTO runs (id, started_at, status, kind, model, "
                    "agent_name, scenario_id, scenario_tags, linked_run_id, fs_trace_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, self.started_at, "running", self.kind, self.model,
                     self.sub_agent, self.scenario_id or None, tags_json,
                     self.linked_run_id or None,
                     self._fs_trace_path or None),
                )
                self._conn.commit()
        except Exception:
            pass

    def _emit_event(self, event_type: str, step: int | None = None, **kw: Any) -> None:
        if self._conn is None:
            return
        try:
            with self._db_lock:
                self._conn.execute(
                    "INSERT INTO events (run_id, agent_id, agent_name, timestamp, event_type, step, data_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, self.run_id, self.sub_agent,
                     datetime.now().isoformat(), event_type, step,
                     json.dumps(kw, default=str, ensure_ascii=False)),
                )
                self._conn.commit()
        except Exception:
            pass
