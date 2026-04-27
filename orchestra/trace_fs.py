"""
Filesystem trace dump (opt-in, in addition to the SQLite store).

Layout under base_dir:

    runs/<run-id>/
        run.json              # top-level: model, pr_url, prompt_hash, started/finished, ...
        events.jsonl          # raw event stream, one JSON object per line, append-only
        agents/
            <name>-<n>/
                meta.json     # agent_id, parent_id, depth, started/finished, status
                step-01.json  # one file per step: messages_in, response, tool_calls, tool_results, usage
                step-02.json
                artifacts/    # free-form dumps via agent.dump_artifact(name, data)

The DB stays primary (fast queries, indexed list). FS is an opt-in
human-readable mirror — easy to grep/jq/diff between runs.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(s: str) -> str:
    return _SAFE_RE.sub("_", s).strip("_") or "unnamed"


def _serialize(value: Any) -> Any:
    """Make value JSON-safe; objects fall back to repr."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


class TraceFSWriter:
    """
    Mirrors EventBus events to a filesystem layout. Same interface as
    TraceDBWriter (on_event, finish_run, close), so cli.py can register
    both without conditional logic.
    """

    def __init__(self, base_dir: str | Path, run_id: str = ""):
        self.run_id = run_id or str(uuid.uuid4())[:12]
        self.base_dir = Path(base_dir).expanduser()
        self.run_dir = self.base_dir / "runs" / self.run_id
        (self.run_dir / "agents").mkdir(parents=True, exist_ok=True)

        self.started_at = datetime.now().isoformat()
        self._lock = threading.Lock()
        self._events_fp = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")

        # Per-agent bookkeeping
        self._agent_dirs: dict[str, Path] = {}        # agent_id -> Path
        self._agent_name_idx: dict[str, int] = {}     # agent_name -> next idx
        self._agent_meta: dict[str, dict] = {}        # agent_id -> meta dict
        self._current_step: dict[str, dict] = {}      # agent_id -> step buffer

        self._write_run_stub()

    # ── Public API ────────────────────────────────────────────────────────────

    def on_event(self, event_type: str, **kw: Any) -> None:
        """Subscribe target — handles every event from EventBus."""
        try:
            self._on_event_inner(event_type, **kw)
        except Exception:
            # Tracing must never crash the agent
            log.debug("trace_fs handler error for %s", event_type, exc_info=True)

    def finish_run(self,
                   model: str = "", pr_url: str = "", diff_summary: str = "",
                   findings_count: int = 0, total_tokens_paid: int = 0,
                   prompt_source: str = "", prompt_hash: str = "",
                   status: str = "completed",
                   **extra: Any) -> None:
        """Update run.json with final fields. Flush remaining step buffers."""
        with self._lock:
            for agent_id in list(self._current_step):
                self._flush_step(agent_id)

            run_data = self._read_run_json()
            run_data.update({
                "finished_at": datetime.now().isoformat(),
                "status": status,
                "model": model or run_data.get("model", ""),
                "pr_url": pr_url or run_data.get("pr_url", ""),
                "diff_summary": diff_summary or run_data.get("diff_summary", ""),
                "findings_count": findings_count,
                "total_tokens_paid": total_tokens_paid,
                "prompt_source": prompt_source or run_data.get("prompt_source", ""),
                "prompt_hash": prompt_hash or run_data.get("prompt_hash", ""),
            })
            for k, v in extra.items():
                run_data[k] = _serialize(v)
            self._write_run_json(run_data)

    def close(self) -> None:
        try:
            self._events_fp.close()
        except Exception:
            pass

    def dump_artifact(self, agent_id: str, name: str, data: Any) -> Optional[Path]:
        """Drop free-form JSON into <agent>/artifacts/<name>.json. Used by AGENT_ARTIFACT events."""
        adir = self._agent_dirs.get(agent_id)
        if not adir:
            return None
        artifacts_dir = adir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        path = artifacts_dir / f"{_safe_name(name)}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_serialize(data), f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            log.debug("dump_artifact failed for %s/%s", agent_id, name, exc_info=True)
            return None
        return path

    # ── Internals ─────────────────────────────────────────────────────────────

    def _write_run_stub(self) -> None:
        with self._lock:
            self._write_run_json({
                "run_id": self.run_id,
                "started_at": self.started_at,
                "status": "running",
            })

    def _read_run_json(self) -> dict:
        path = self.run_dir / "run.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _write_run_json(self, data: dict) -> None:
        path = self.run_dir / "run.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)

    def _agent_dir(self, agent_id: str, agent_name: str) -> Path:
        if agent_id in self._agent_dirs:
            return self._agent_dirs[agent_id]
        idx = self._agent_name_idx.get(agent_name, 0)
        self._agent_name_idx[agent_name] = idx + 1
        folder = self.run_dir / "agents" / f"{_safe_name(agent_name)}-{idx}"
        folder.mkdir(parents=True, exist_ok=True)
        self._agent_dirs[agent_id] = folder
        return folder

    def _on_event_inner(self, event_type: str, **kw: Any) -> None:
        # 1) Always append to events.jsonl
        with self._lock:
            line = json.dumps({
                "ts": datetime.now().isoformat(),
                "event": event_type,
                **{k: _serialize(v) for k, v in kw.items() if k not in ("agent", "event_bus")},
            }, ensure_ascii=False, default=str)
            self._events_fp.write(line + "\n")
            self._events_fp.flush()

        agent_id = kw.get("agent_id")
        if not agent_id:
            return
        agent_name = kw.get("agent_name", "agent")

        if event_type == "agent_started":
            adir = self._agent_dir(agent_id, agent_name)
            meta = {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "parent_id": kw.get("parent_id", ""),
                "depth": kw.get("depth", 0),
                "started_at": datetime.now().isoformat(),
                "status": "running",
            }
            self._agent_meta[agent_id] = meta
            self._write_agent_meta(adir, meta)
            return

        # Lazy-create agent dir if started event was missed
        adir = self._agent_dir(agent_id, agent_name)

        if event_type == "agent_step":
            # New step starting — flush previous one for this agent
            with self._lock:
                self._flush_step(agent_id)
                self._current_step[agent_id] = {
                    "step": kw.get("step"),
                    "started_at": datetime.now().isoformat(),
                    "llm_params": _serialize(kw.get("llm_params")),
                    "tool_calls_planned": _serialize(kw.get("tool_calls", [])),
                }
            return

        if event_type == "agent_llm_request":
            with self._lock:
                buf = self._current_step.setdefault(agent_id, {})
                buf["llm_request"] = {
                    "messages": _serialize(kw.get("messages", [])),
                    "tools": _serialize(kw.get("tools", [])),
                    "model": kw.get("model"),
                }
            return

        if event_type == "agent_llm_response":
            with self._lock:
                buf = self._current_step.setdefault(agent_id, {})
                buf["llm_response"] = {
                    "content": _serialize(kw.get("content", "")),
                    "tool_calls": _serialize(kw.get("tool_calls", [])),
                    "usage": _serialize(kw.get("usage")),
                }
            return

        if event_type == "agent_tool_result":
            with self._lock:
                buf = self._current_step.setdefault(agent_id, {})
                buf.setdefault("tool_results", []).append({
                    "tool": kw.get("tool"),
                    "args": _serialize(kw.get("args")),
                    "result_preview": kw.get("result_preview"),
                    "result_len": kw.get("result_len"),
                    "result_count": kw.get("result_count"),
                })
            return

        if event_type == "agent_reflect":
            with self._lock:
                buf = self._current_step.setdefault(agent_id, {})
                buf.setdefault("reflects", []).append({
                    "confidence": kw.get("confidence"),
                    "reasoning": _serialize(kw.get("reasoning", "")),
                    "concerns": _serialize(kw.get("concerns", [])),
                    "open_questions": _serialize(kw.get("open_questions", [])),
                })
            return

        if event_type in ("agent_done", "agent_forced_done"):
            with self._lock:
                self._flush_step(agent_id)
                meta = self._agent_meta.setdefault(agent_id, {
                    "agent_id": agent_id, "agent_name": agent_name,
                })
                meta.update({
                    "finished_at": datetime.now().isoformat(),
                    "status": "forced_done" if event_type == "agent_forced_done" else "done",
                    "tokens_in": kw.get("tok_in"),
                    "tokens_out": kw.get("tok_out"),
                    "tokens_cached": kw.get("tok_cached"),
                    "reason": kw.get("reason", ""),
                })
                self._write_agent_meta(adir, meta)
            return

        if event_type == "agent_artifact":
            self.dump_artifact(agent_id, kw.get("name", "artifact"), kw.get("data"))
            return

    def _flush_step(self, agent_id: str) -> None:
        buf = self._current_step.pop(agent_id, None)
        if not buf:
            return
        adir = self._agent_dirs.get(agent_id)
        if not adir:
            return
        step_n = buf.get("step")
        if step_n is None:
            return
        path = adir / f"step-{int(step_n):02d}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(buf, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            log.debug("flush_step failed for %s step %s", agent_id, step_n, exc_info=True)

    def _write_agent_meta(self, adir: Path, meta: dict) -> None:
        path = adir / "meta.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            log.debug("write_agent_meta failed", exc_info=True)
