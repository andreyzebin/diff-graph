"""
FastAPI trace server with WebSocket live updates.

Usage:
    python cli.py serve              # start on localhost:8080
    python cli.py serve --port 9000  # custom port
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..trace_db import TraceDBReader, DEFAULT_DB_PATH
from ..trace import render_html

_DIR = Path(__file__).parent

app = FastAPI(title="DiffGraph Trace Viewer")
app.mount("/static", StaticFiles(directory=_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_DIR / "templates")


def _get_reader() -> TraceDBReader:
    if not DEFAULT_DB_PATH.exists():
        raise FileNotFoundError("No trace DB")
    return TraceDBReader()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """List all runs."""
    try:
        reader = _get_reader()
        runs = reader.list_runs(limit=50)
        reader.close()
    except FileNotFoundError:
        runs = []
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str):
    """Render trace — full if completed, live page if running."""
    reader = _get_reader()
    runs = reader.list_runs(limit=100)
    run_meta = next((r for r in runs if r["id"] == run_id), {})
    status = run_meta.get("status", "completed")

    if status == "running":
        # Serve live page with WebSocket
        reader.close()
        return templates.TemplateResponse(request, "live.html", {
            "run_id": run_id,
            "run_meta": run_meta,
        })

    # Completed — full trace render
    trace_data = reader.get_run_trace(run_id)
    reader.close()
    html = render_html(trace_data, title=f"Trace · {run_id[:12]}")
    return HTMLResponse(content=html)


@app.get("/runs/{run_id}/json")
async def run_json(run_id: str):
    """Raw trace data as JSON."""
    reader = _get_reader()
    trace_data = reader.get_run_trace(run_id)
    reader.close()
    return JSONResponse(content=trace_data)


@app.websocket("/ws/live/{run_id}")
async def ws_live(websocket: WebSocket, run_id: str):
    """Push new events to browser in real-time."""
    await websocket.accept()

    last_event_id = 0

    try:
        while True:
            # Read new events from SQLite
            conn = sqlite3.connect(str(DEFAULT_DB_PATH))
            conn.row_factory = sqlite3.Row

            new_events = conn.execute(
                "SELECT id, agent_id, agent_name, event_type, step, data_json, timestamp "
                "FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT 50",
                (run_id, last_event_id),
            ).fetchall()

            # Check run status
            run_row = conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            run_status = run_row["status"] if run_row else "unknown"

            conn.close()

            # Send new events
            for ev in new_events:
                data = json.loads(ev["data_json"]) if ev["data_json"] else {}
                await websocket.send_json({
                    "id": ev["id"],
                    "agent_id": ev["agent_id"] or "",
                    "agent_name": ev["agent_name"] or "",
                    "event_type": ev["event_type"],
                    "step": ev["step"],
                    "timestamp": ev["timestamp"],
                    "data": data,
                })
                last_event_id = ev["id"]

            # Check if run completed
            if run_status in ("completed", "failed") and not new_events:
                await websocket.send_json({"event_type": "run_complete", "status": run_status})
                break

            await asyncio.sleep(0.5)

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


def create_app() -> FastAPI:
    return app
