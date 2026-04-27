"""
Filesystem trace dump (opt-in, in addition to the SQLite store).

API-boundary tracing: we capture the two API edges that matter —
LLM API (us ↔ LLM) and Tool API (LLM ↔ our tools). Each call writes
its request before going out and its response after returning. Crash
between the two = request file still on disk.

Layout under base_dir:

    runs/<run-id>/
        run.json                                # top-level metadata
        events.jsonl                            # full event stream, append-only
        agents/
            <name>-<n>/
                meta.json                       # agent_id, parent_id, depth, started/finished
                step-NN-request.json            # LLM request (messages, tools, params)
                step-NN-response.json           # LLM response (content, tool_calls, usage)
                step-NN-tool-SS-request.json    # Tool request (name, args, tool_call_id)
                step-NN-tool-SS-response.json   # Tool response (full content)
                artifacts/                      # free-form dumps via agent.dump_artifact

NN = LLM step number, SS = tool sequence within that step.
The DB stays primary for cross-run queries. FS is the inspectable mirror.
"""
from __future__ import annotations

import json
import logging
import re
import threading
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

    def __init__(self, run_dir: str | Path):
        # The caller passes the exact directory to write into. No implicit
        # `runs/<uuid>/` nesting — that's the caller's job (cli.py wraps
        # standalone invocations, benchmark dictates its own layout).
        self.run_dir = Path(run_dir).expanduser()
        (self.run_dir / "agents").mkdir(parents=True, exist_ok=True)

        self.run_id = self.run_dir.name
        self.started_at = datetime.now().isoformat()
        self._lock = threading.Lock()
        self._events_fp = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")

        # Per-agent bookkeeping
        self._agent_dirs: dict[str, Path] = {}        # agent_id -> Path
        self._agent_name_idx: dict[str, int] = {}     # agent_name -> next idx
        self._agent_meta: dict[str, dict] = {}        # agent_id -> meta dict

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
        """Update run.json with final fields."""
        with self._lock:
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

        # ── Agent lifecycle → meta.json ───────────────────────────────────
        if event_type in ("agent_done", "agent_forced_done"):
            with self._lock:
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

        # ── LLM API boundary: exactly two files per step ──────────────────
        step_n = kw.get("step")
        if step_n is None:
            return

        if event_type == "agent_llm_request":
            payload = {
                "step": step_n,
                "ts": datetime.now().isoformat(),
                "messages": _serialize(kw.get("messages", [])),
                "tools": _serialize(kw.get("tools", [])),
                "llm_params": _serialize(kw.get("llm_params")),
            }
            self._atomic_write(adir / f"step-{int(step_n):02d}-request.json", payload)
            return

        if event_type == "agent_llm_response":
            payload = {
                "step": step_n,
                "ts": datetime.now().isoformat(),
                "content": _serialize(kw.get("content", "")),
                "tool_calls": _serialize(kw.get("tool_calls", [])),
                "usage": _serialize(kw.get("usage")),
            }
            self._atomic_write(adir / f"step-{int(step_n):02d}-response.json", payload)
            return

        # ── Tool API boundary ─────────────────────────────────────────────
        seq = kw.get("seq")
        if event_type == "agent_tool_request" and seq is not None:
            payload = {
                "step": step_n,
                "seq": seq,
                "ts": datetime.now().isoformat(),
                "tool": kw.get("tool"),
                "tool_call_id": kw.get("tool_call_id"),
                "args": _serialize(kw.get("args")),
            }
            self._atomic_write(
                adir / f"step-{int(step_n):02d}-tool-{int(seq):02d}-request.json",
                payload,
            )
            return

        if event_type == "agent_tool_response" and seq is not None:
            payload = {
                "step": step_n,
                "seq": seq,
                "ts": datetime.now().isoformat(),
                "tool": kw.get("tool"),
                "tool_call_id": kw.get("tool_call_id"),
                "content": _serialize(kw.get("content", "")),
                "content_len": kw.get("content_len"),
            }
            self._atomic_write(
                adir / f"step-{int(step_n):02d}-tool-{int(seq):02d}-response.json",
                payload,
            )
            return
        # Other step-tagged events (agent_step, agent_reflect, agent_tool_result
        # preview) are captured implicitly inside the next LLM request.

    def _atomic_write(self, path: Path, data: Any) -> None:
        """Write JSON atomically: tmp file + rename. Survives mid-write crashes."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(path)
        except Exception:
            log.debug("atomic_write failed: %s", path, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _write_agent_meta(self, adir: Path, meta: dict) -> None:
        path = adir / "meta.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            log.debug("write_agent_meta failed", exc_info=True)
