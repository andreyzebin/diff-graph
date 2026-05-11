"""
SQLite trace storage.

TraceDBWriter subscribes to EventBus and persists every event in real-time.
Survives crashes — partial traces are queryable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_DB_PATH = Path.home() / ".diffgraph" / "traces.db"


class TraceDBWriter:
    """Writes events to SQLite in real-time.

    The optional `kind` parameter tags the run as 'agent' (default —
    diff-graph's own agentic ReAct loop), 'judge' (single-shot
    scoring call from the bench) or 'evaluator' (future debugger
    sub-agents that read traces). All three share the same `runs` /
    `events` tables; `kind` is just a filterable column.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, run_id: str = "",
                 kind: str = "agent"):
        import threading
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.run_id = run_id or str(uuid.uuid4())[:12]
        self.kind = kind
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
        # Migrate: add columns if missing (existing DBs).
        # Each column is a separate try/except so we can add new ones
        # without disturbing the existing migration order.
        for col, decl in [
            ("prompt_source", "TEXT"),
            ("prompt_hash", "TEXT"),
            ("tags", "TEXT"),
            ("kind", "TEXT DEFAULT 'agent'"),
            # 5e.11 — first-class search dimensions
            ("agent_name", "TEXT"),
            ("generation", "TEXT"),
            ("mutation", "TEXT"),
            ("genes", "TEXT"),                # JSON array — legacy column, no longer written; kept to avoid migration
            ("project", "TEXT"),
            ("files_touched", "TEXT"),        # JSON array
            ("jira_keys", "TEXT"),            # JSON array
            ("scenario_id", "TEXT"),
            ("scenario_tags", "TEXT"),        # JSON array
            ("linked_run_id", "TEXT"),
            ("duration_ms", "INTEGER"),
            ("fs_trace_path", "TEXT"),
        ]:
            try:
                self.conn.execute(f"SELECT {col} FROM runs LIMIT 0")
            except sqlite3.OperationalError:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

        # Indices for the search dimensions (5e.11)
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_runs_kind        ON runs(kind);
            CREATE INDEX IF NOT EXISTS idx_runs_started     ON runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_mutation    ON runs(mutation);
            CREATE INDEX IF NOT EXISTS idx_runs_generation  ON runs(generation);
            CREATE INDEX IF NOT EXISTS idx_runs_agent       ON runs(agent_name);
            CREATE INDEX IF NOT EXISTS idx_runs_project     ON runs(project);
            CREATE INDEX IF NOT EXISTS idx_runs_scenario    ON runs(scenario_id);
            CREATE INDEX IF NOT EXISTS idx_runs_linked      ON runs(linked_run_id);
            CREATE INDEX IF NOT EXISTS idx_runs_status      ON runs(status);
        """)

    def _insert_run(self):
        # OR IGNORE: when the worker pre-stamps DIFFGRAPH_TRACE_RUN_ID on
        # the agent's qa_task, the bench process inherits that env and
        # passes it to BOTH the agent's cli.py AND the judge's cli.py
        # (via OrchestraJudge). The second invocation hits a UNIQUE
        # constraint on runs.id; ignoring is safe because finish_run
        # uses UPDATE … WHERE id=? and events/spans are addressed by
        # span_id (unique by construction).
        self.conn.execute(
            "INSERT OR IGNORE INTO runs (id, started_at, status, kind) "
            "VALUES (?, ?, ?, ?)",
            (self.run_id, datetime.now(timezone.utc).isoformat(),
             "running", self.kind),
        )
        self.conn.commit()

    def set_prompt_info(self, prompt_source: str, prompt_hash: str):
        """Set prompt source/hash early (before run finishes).

        mutation is COALESCE'd so calls to `override_mutation()` (e.g.
        DIFFGRAPH_MUTATION_OVERRIDE for git-commit-level tracking)
        survive regardless of order. Calling order is:
          - set_prompt_info first  → mutation=prompt_hash
          - override_mutation later → mutation=git_sha
          OR
          - override_mutation first → mutation=git_sha
          - set_prompt_info later  → mutation stays git_sha (COALESCE)
          OR
          - finish_run last → mutation untouched if already set.
        """
        with self._lock:
            generation = self._derive_generation(prompt_source)
            self.conn.execute(
                "UPDATE runs SET prompt_source=?, prompt_hash=?, "
                "generation=COALESCE(generation, ?), "
                "mutation=COALESCE(mutation, ?) "
                "WHERE id=?",
                (prompt_source, prompt_hash, generation, prompt_hash, self.run_id),
            )
            self.conn.commit()

    def override_mutation(self, value: str) -> None:
        """Replace the mutation column on this run with `value` (e.g.
        a git commit SHA). Caller authoritative — overwrites any
        prior mutation.

        Used by quality-cli worker to tag agent runs with the auto-
        plan task's commit hash so analytics can group by commit
        rather than by prompt-content-hash.
        """
        if not value:
            return
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE runs SET mutation=? WHERE id=?",
                    (value[:7], self.run_id),
                )
                self.conn.commit()
        except Exception:
            pass

    @staticmethod
    def _derive_generation(prompt_source: str) -> str:
        """Extract a stable generation id from a prompt source URI.

        Examples:
          "diffgraph/prompts" → "prompts"
          "file:///.../prompts-experimental" → "prompts-experimental"
          "bitbucket://server/SBLOOM/diffgraph-prompts/refs/main/prompts" → "prompts"
        """
        if not prompt_source:
            return ""
        s = str(prompt_source).rstrip("/")
        return s.rsplit("/", 1)[-1] if "/" in s else s

    def set_search_metadata(self, *,
                            agent_name: str = "",
                            project: str = "",
                            files_touched: list[str] | None = None,
                            jira_keys: list[str] | None = None,
                            scenario_id: str = "",
                            scenario_tags: list[str] | None = None,
                            linked_run_id: str = "",
                            fs_trace_path: str = "") -> None:
        """Populate the search-dimension columns on the runs row.

        Best-effort: tracing must never crash the agent. Any subset of
        the fields can be passed; unset fields are left alone.
        """
        try:
            updates = []
            params = []
            if agent_name:
                updates.append("agent_name=?")
                params.append(agent_name)
            if project:
                updates.append("project=?")
                params.append(project)
            if files_touched is not None:
                updates.append("files_touched=?")
                params.append(json.dumps(files_touched, ensure_ascii=False))
            if jira_keys is not None:
                updates.append("jira_keys=?")
                params.append(json.dumps(jira_keys, ensure_ascii=False))
            if scenario_id:
                updates.append("scenario_id=?")
                params.append(scenario_id)
            if scenario_tags is not None:
                updates.append("scenario_tags=?")
                params.append(json.dumps(scenario_tags, ensure_ascii=False))
            if linked_run_id:
                updates.append("linked_run_id=?")
                params.append(linked_run_id)
            if fs_trace_path:
                updates.append("fs_trace_path=?")
                params.append(fs_trace_path)
            if not updates:
                return
            params.append(self.run_id)
            with self._lock:
                self.conn.execute(
                    f"UPDATE runs SET {', '.join(updates)} WHERE id=?",
                    params,
                )
                self.conn.commit()
        except Exception:
            pass

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
                    (self.run_id, aid, aname, datetime.now(timezone.utc).isoformat(), event_type, step,
                     json.dumps(data, default=str, ensure_ascii=False)),
                )
                self.conn.commit()
        except Exception:
            pass  # never crash the agent due to tracing

    def finish_run(self, model: str = "", pr_url: str = "", diff_summary: str = "",
                   findings_count: int = 0, total_tokens_paid: int = 0,
                   prompt_source: str = "", prompt_hash: str = "",
                   status: str = "completed"):
        """Mark run as `completed` (default) or `failed` / `cancelled`.

        Callers in cli.py's `finally` use status='failed' so a run
        terminated by SIGTERM/Exception doesn't leave the row stuck
        at status='running' forever (see the orphan sweeper in
        tracing/server for the safety net on this same invariant).
        """
        # Compute duration_ms from started_at (if present).
        duration_ms = None
        try:
            row = self.conn.execute(
                "SELECT started_at FROM runs WHERE id=?", (self.run_id,)
            ).fetchone()
            if row and row[0]:
                started = datetime.fromisoformat(row[0])
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        except Exception:
            duration_ms = None

        # Extract search-dimension fields from pr_url + diff_summary
        # (pure derivation — best effort).
        project = _extract_project(pr_url)
        files_touched = _extract_files_from_diff(diff_summary)

        generation = self._derive_generation(prompt_source)
        with self._lock:
            # mutation is set once at run-start (set_prompt_info) and may
            # be overridden by callers (e.g. DIFFGRAPH_MUTATION_OVERRIDE
            # for git-commit-level QA tracking). Use `COALESCE(existing,
            # incoming)` so finish_run only writes a value when none
            # exists — never undoes an override.
            self.conn.execute(
                "UPDATE runs SET finished_at=?, model=?, pr_url=?, diff_summary=?, "
                "findings_count=?, total_tokens_paid=?, status=?, "
                "prompt_source=COALESCE(NULLIF(?, ''), prompt_source), "
                "prompt_hash=COALESCE(NULLIF(?, ''), prompt_hash), "
                "generation=COALESCE(NULLIF(?, ''), generation), "
                "mutation=COALESCE(mutation, NULLIF(?, '')), "
                "duration_ms=?, "
                "project=COALESCE(NULLIF(?, ''), project), "
                "files_touched=COALESCE(NULLIF(?, '[]'), files_touched) "
                "WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), model, pr_url, diff_summary,
                 findings_count, total_tokens_paid, status,
                 prompt_source, prompt_hash, generation, prompt_hash,
                 duration_ms,
                 project,
                 json.dumps(files_touched, ensure_ascii=False),
                 self.run_id),
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
        try:
            self.conn.execute("SELECT tags FROM runs LIMIT 0")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE runs ADD COLUMN tags TEXT")

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


# ── Search-dimension extractors (5e.11) ─────────────────────────────────────
# Pure functions, used by both the writer (denormalises into runs row at
# finish time) and any external caller wiring tracing.

import re as _re


_PR_URL_PROJECT_RE = _re.compile(r"/projects/([^/]+)/repos/", _re.IGNORECASE)
_DIFF_FILE_RE = _re.compile(r"^[+-]{3} (?:[ab]/)?(.+?)(?:\s+\(\w+\))?\s*$", _re.MULTILINE)


def _extract_project(pr_url: str) -> str:
    """Extract Bitbucket project key from a PR URL.

    Examples:
      "https://bitbucket-ci/projects/SBLOOM/repos/foo/pull-requests/123" → "SBLOOM"
      "" → ""
    """
    if not pr_url:
        return ""
    m = _PR_URL_PROJECT_RE.search(str(pr_url))
    return m.group(1) if m else ""


def _extract_files_from_diff(diff_summary: str) -> list[str]:
    """Extract file paths from a diff_summary blob.

    diff_summary is a render of the diff (possibly truncated). We
    scan for `+++ b/path` and `--- a/path` markers and unique-ify
    while preserving order. Returns up to 50 paths to keep the JSON
    blob bounded.
    """
    if not diff_summary:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _DIFF_FILE_RE.finditer(str(diff_summary)):
        path = m.group(1).strip()
        if path == "/dev/null" or not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= 50:
            break
    return out


_JIRA_KEY_RE = _re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
_JIRA_KEY_DENYLIST = {"HTTP", "HTTPS", "JIRA", "TODO", "FIXME", "NOTE", "BUG"}


def _extract_jira_keys(text: str, project_whitelist: list[str] | None = None) -> list[str]:
    """Extract Jira-style ticket keys from arbitrary text.

    `project_whitelist` is recommended in production to filter false
    positives like `HTTP-404`. When None, denylist of common false
    positives is used.

    Returns up to 20 unique keys, in order of first appearance.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    whitelist = set(project_whitelist) if project_whitelist else None
    for m in _JIRA_KEY_RE.finditer(str(text)):
        proj = m.group(1)
        if whitelist is not None:
            if proj not in whitelist:
                continue
        elif proj in _JIRA_KEY_DENYLIST:
            continue
        key = f"{proj}-{m.group(2)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= 20:
            break
    return out
