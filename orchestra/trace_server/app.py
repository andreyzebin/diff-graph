"""
FastAPI trace server for browsing review traces.

Usage:
    python cli.py serve              # start on localhost:8080
    python cli.py serve --port 9000  # custom port
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
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
async def run_detail(run_id: str):
    """Render trace for a specific run."""
    reader = _get_reader()
    trace_data = reader.get_run_trace(run_id)

    # Get run metadata
    runs = reader.list_runs(limit=100)
    run_meta = next((r for r in runs if r["id"] == run_id), {})
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


def create_app() -> FastAPI:
    return app
