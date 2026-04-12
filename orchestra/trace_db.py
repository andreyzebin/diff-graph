"""
SQLite trace storage.

TraceDBWriter subscribes to EventBus and persists every event in real-time.
Survives crashes — partial traces are queryable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


DEFAULT_DB_PATH = Path.home() / ".diffgraph" / "traces.db"


class TraceDBWriter:
    """Writes events to SQLite in real-time."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, run_id: str = ""):
        import threading
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.run_id = run_id or str(uuid.uuid4())[:12]
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._create_tables()
        self._insert_run()

    def _create_tables(self):
        self.conn.executescript("""
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
        # Migrate: add columns if missing (existing DBs)
        try:
            self.conn.execute("SELECT prompt_source FROM runs LIMIT 0")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE runs ADD COLUMN prompt_source TEXT")
            self.conn.execute("ALTER TABLE runs ADD COLUMN prompt_hash TEXT")

    def _insert_run(self):
        self.conn.execute(
            "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
            (self.run_id, datetime.now().isoformat(), "running"),
        )
        self.conn.commit()

    def set_prompt_info(self, prompt_source: str, prompt_hash: str):
        """Set prompt source/hash early (before run finishes)."""
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET prompt_source=?, prompt_hash=? WHERE id=?",
                (prompt_source, prompt_hash, self.run_id),
            )
            self.conn.commit()

    def on_event(self, event_type: str, **kw):
        """Called on every event. Writes to DB immediately."""
        aid = kw.get("agent_id", "") or kw.get("parent_id", "")
        aname = kw.get("agent_name", "")
        step = kw.get("step")

        # Serialize data — handle non-serializable objects, skip huge fields
        data = {}
        for k, v in kw.items():
            if k in ("agent", "event_bus"):  # skip object references
                continue
            if k == "messages" and isinstance(v, list):
                # Compact messages: truncate content
                compact = []
                for m in v:
                    cm = {"role": m.get("role", "?")}
                    content = m.get("content", "")
                    if content:
                        cm["content"] = content
                    if m.get("tool_calls"):
                        cm["tool_calls"] = m["tool_calls"]
                    compact.append(cm)
                data[k] = compact
                continue
            if k == "tools" and isinstance(v, list):
                data["tools_count"] = len(v)
                continue
            try:
                json.dumps(v)
                data[k] = v
            except (TypeError, ValueError):
                data[k] = str(v)

        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO events (run_id, agent_id, agent_name, timestamp, event_type, step, data_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, aid, aname, datetime.now().isoformat(), event_type, step,
                     json.dumps(data, default=str, ensure_ascii=False)),
                )
                self.conn.commit()
        except Exception:
            pass  # never crash the agent due to tracing

    def finish_run(self, model: str = "", pr_url: str = "", diff_summary: str = "",
                   findings_count: int = 0, total_tokens_paid: int = 0,
                   prompt_source: str = "", prompt_hash: str = ""):
        """Mark run as completed."""
        self.conn.execute(
            "UPDATE runs SET finished_at=?, model=?, pr_url=?, diff_summary=?, "
            "findings_count=?, total_tokens_paid=?, status='completed', "
            "prompt_source=?, prompt_hash=? WHERE id=?",
            (datetime.now().isoformat(), model, pr_url, diff_summary,
             findings_count, total_tokens_paid,
             prompt_source, prompt_hash, self.run_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class TraceDBReader:
    """Reads trace data from SQLite for rendering."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"No trace DB at {self.db_path}")
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        # Migrate: add columns if missing (existing DBs)
        try:
            self.conn.execute("SELECT prompt_source FROM runs LIMIT 0")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE runs ADD COLUMN prompt_source TEXT")
            self.conn.execute("ALTER TABLE runs ADD COLUMN prompt_hash TEXT")

    def list_runs(self, limit: int = 10) -> list[dict]:
        """List recent runs."""
        rows = self.conn.execute(
            "SELECT id, started_at, finished_at, model, pr_url, diff_summary, "
            "findings_count, total_tokens_paid, status, prompt_source, prompt_hash "
            "FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run_trace(self, run_id: str) -> dict:
        """Build trace tree from events for a given run."""
        events = self.conn.execute(
            "SELECT agent_id, agent_name, event_type, step, data_json "
            "FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()

        # Group events by agent
        agents: dict[str, dict] = {}  # agent_id -> trace dict
        agent_parents: dict[str, str] = {}  # child_id -> parent_id
        root_id = ""

        for ev in events:
            aid = ev["agent_id"]
            aname = ev["agent_name"]
            etype = ev["event_type"]
            step = ev["step"]
            data = json.loads(ev["data_json"]) if ev["data_json"] else {}

            if aid and aid not in agents:
                agents[aid] = {
                    "agent_id": aid,
                    "agent_name": aname or aid[:8],
                    "steps": 0,
                    "tokens_in": 0, "tokens_out": 0,
                    "tokens_cached": 0, "tokens_paid": 0,
                    "sgr": [],
                    "llm_calls": [],
                    "children": [],
                    "output": None,
                }

            if etype == "agent_started" and not root_id:
                root_id = aid

            if etype == "agent_spawned":
                child_id = data.get("child_id", "")
                parent_id = data.get("parent_id", "") or aid
                child_name = data.get("agent_name", "")
                # Register child agent even before it emits its own events
                if child_id and child_id not in agents:
                    agents[child_id] = {
                        "agent_id": child_id,
                        "agent_name": child_name or child_id[:8],
                        "steps": 0,
                        "tokens_in": 0, "tokens_out": 0,
                        "tokens_cached": 0, "tokens_paid": 0,
                        "sgr": [],
                            "llm_calls": [],
                        "children": [],
                        "output": None,
                    }
                if child_id and parent_id:
                    agent_parents[child_id] = parent_id

            if not aid or aid not in agents:
                continue

            agent = agents[aid]

            if etype == "agent_llm_request":
                agent["llm_calls"].append({
                    "step": step,
                    "type": "request",
                    "messages": data.get("messages", []),
                    "message_count": len(data.get("messages", [])),
                    "llm_params": data.get("llm_params", {}),
                    "tools_count": data.get("tools_count", 0),
                })
            elif etype == "agent_llm_response":
                agent["llm_calls"].append({
                    "step": step,
                    "type": "response",
                    "tool_calls": data.get("tool_calls", []),
                    "content": data.get("content", ""),
                    "usage": data.get("usage", {}),
                })
                usage = data.get("usage", {})
                current_paid = usage.get("paid", 0)
                # Track last values to compute deltas
                prev_paid = agent.get("_last_paid", 0)
                delta = current_paid - prev_paid if current_paid > prev_paid else current_paid
                agent["_last_paid"] = current_paid
                agent["tokens_paid"] += delta
                agent["tokens_out"] += usage.get("completion_tokens", 0)
            elif etype == "agent_reflect":
                agent["sgr"].append({
                    "step": step,
                    "learned": data.get("learned", ""),
                    "questions_remaining": data.get("questions_remaining", []),
                    "resolved_questions": data.get("resolved_questions", []),
                    "confidence": data.get("confidence", ""),
                    "next_action": data.get("next_action", ""),
                })
            elif etype in ("agent_step", "agent_tool_result"):
                if step is not None:
                    agent["steps"] = max(agent["steps"], step + 1)
            elif etype in ("agent_done", "orchestrator_done"):
                output = data.get("output")
                if output:
                    agent["output"] = output

        # Build tree: link children to parents
        linked = set()
        for child_id, parent_id in agent_parents.items():
            if child_id in agents and parent_id in agents:
                agents[parent_id]["children"].append(agents[child_id])
                linked.add(child_id)

        # Orphan agents (events exist but no agent_spawned) → attach to root
        if root_id:
            for aid in agents:
                if aid != root_id and aid not in linked:
                    agents[root_id]["children"].append(agents[aid])

        # Return root
        if root_id and root_id in agents:
            return agents[root_id]

        # Fallback: return first agent
        if agents:
            return next(iter(agents.values()))
        return {"agent_id": "", "agent_name": "unknown", "steps": 0,
                "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
                "tokens_paid": 0, "sgr": [], "llm_calls": [],
                "children": [], "output": None}

    def get_last_run_id(self) -> Optional[str]:
        """Get the most recent run ID."""
        row = self.conn.execute(
            "SELECT id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def close(self):
        self.conn.close()
