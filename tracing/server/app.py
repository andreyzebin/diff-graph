"""
FastAPI trace server with WebSocket live updates and data API.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from orchestra.trace_db import TraceDBReader, DEFAULT_DB_PATH
from orchestra.trace import prepare_for_template

_DIR = Path(__file__).parent
BASE_PATH = os.environ.get("TRACE_BASE_PATH", "").rstrip("/")

app = FastAPI(title="DiffGraph Trace Viewer", root_path=BASE_PATH)
app.mount(f"{BASE_PATH}/static", StaticFiles(directory=_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_DIR / "templates")
# Inject base_path into all template contexts
templates.env.globals["base_path"] = BASE_PATH


def _get_reader() -> TraceDBReader:
    if not DEFAULT_DB_PATH.exists():
        raise FileNotFoundError("No trace DB")
    return TraceDBReader()


# ── Pages ─────────────────────────────────────────────────────────────────────

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


@app.get("/runs/{run_id}")
async def run_detail(run_id: str):
    """Redirect to live if running, trace if completed."""
    reader = _get_reader()
    runs = reader.list_runs(limit=100)
    run_meta = next((r for r in runs if r["id"] == run_id), {})
    reader.close()
    if run_meta.get("status") == "running":
        return RedirectResponse(f"{BASE_PATH}/runs/{run_id}/live", status_code=302)
    return RedirectResponse(f"{BASE_PATH}/runs/{run_id}/trace", status_code=302)


@app.get("/runs/{run_id}/live", response_class=HTMLResponse)
async def run_live(request: Request, run_id: str):
    """Live event stream view."""
    reader = _get_reader()
    runs = reader.list_runs(limit=100)
    run_meta = next((r for r in runs if r["id"] == run_id), {})
    reader.close()
    return templates.TemplateResponse(request, "live.html", {
        "run_id": run_id,
        "run_meta": run_meta,
    })


@app.get("/runs/{run_id}/trace", response_class=HTMLResponse)
async def run_trace(request: Request, run_id: str):
    """Trace navigator (snapshot)."""
    reader = _get_reader()
    trace_data = reader.get_run_trace(run_id)
    reader.close()
    template_data = prepare_for_template(trace_data)
    return templates.TemplateResponse(request, "trace.html", {
        "title": run_id[:12],
        "run_id": run_id,
        "trace": template_data,
    })


# ── Data API ──────────────────────────────────────────────────────────────────

@app.get("/api/runs")
async def api_runs():
    """List runs as JSON (for polling)."""
    try:
        reader = _get_reader()
        runs = reader.list_runs(limit=50)
        reader.close()
    except FileNotFoundError:
        runs = []
    return JSONResponse(content=runs)


@app.get("/api/metrics")
async def api_metrics(hash: str, since: str = None):
    """Aggregate metrics for a prompt hash."""
    from tracing.query import get_metrics
    m = get_metrics(hash, since=since)
    return JSONResponse(content=m.to_dict())


@app.get("/api/compare")
async def api_compare(a: str, b: str):
    """Compare two prompt hashes."""
    from tracing.query import compare
    c = compare(a, b)
    return JSONResponse(content=c.to_dict())


@app.get("/api/runs/{run_id}/json")
async def api_run_json(run_id: str):
    """Full trace data as JSON."""
    reader = _get_reader()
    trace_data = reader.get_run_trace(run_id)
    reader.close()
    return JSONResponse(content=trace_data)


@app.get("/api/runs/{run_id}/step/{agent_id}/{step}/messages")
async def api_step_messages(run_id: str, agent_id: str, step: int):
    """Full messages array for a specific step (for [⧉] on-demand loading)."""
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT data_json FROM events WHERE run_id=? AND agent_id=? AND event_type='agent_llm_request' AND step=? LIMIT 1",
        (run_id, agent_id, step),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse(content={"error": "not found"}, status_code=404)
    data = json.loads(row["data_json"]) if row["data_json"] else {}
    return JSONResponse(content=data.get("messages", []))


@app.get("/api/runs/{run_id}/step/{agent_id}/{step}/call")
async def api_step_call(run_id: str, agent_id: str, step: int):
    """Full tool call arguments for a specific step."""
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT data_json FROM events WHERE run_id=? AND agent_id=? AND event_type='agent_llm_response' AND step=? LIMIT 1",
        (run_id, agent_id, step),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse(content={"error": "not found"}, status_code=404)
    data = json.loads(row["data_json"]) if row["data_json"] else {}
    return JSONResponse(content=data.get("tool_calls", []))


@app.get("/api/runs/{run_id}/step/{agent_id}/{step}/result", response_class=PlainTextResponse)
async def api_step_result(run_id: str, agent_id: str, step: int):
    """Full tool result for a specific step (delta tool messages only)."""
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row

    # Count tool messages in current step's request (= offset for previous steps)
    cur_row = conn.execute(
        "SELECT data_json FROM events WHERE run_id=? AND agent_id=? AND event_type='agent_llm_request' AND step=? LIMIT 1",
        (run_id, agent_id, step),
    ).fetchone()
    prev_tool_count = 0
    if cur_row:
        cur_data = json.loads(cur_row["data_json"]) if cur_row["data_json"] else {}
        prev_tool_count = sum(1 for m in cur_data.get("messages", []) if m.get("role") == "tool")

    # Get next step's request to find new tool messages
    next_row = conn.execute(
        "SELECT data_json FROM events WHERE run_id=? AND agent_id=? AND event_type='agent_llm_request' AND step>? ORDER BY step LIMIT 1",
        (run_id, agent_id, step),
    ).fetchone()
    conn.close()
    if not next_row:
        return PlainTextResponse("(no result captured)", status_code=404)
    data = json.loads(next_row["data_json"]) if next_row["data_json"] else {}
    messages = data.get("messages", [])
    tool_msgs = [m.get("content", "") for m in messages if m.get("role") == "tool"]
    # Only return NEW tool messages (delta from current step)
    new_msgs = tool_msgs[prev_tool_count:]
    return PlainTextResponse("\n---\n".join(new_msgs) if new_msgs else "(no tool results)")


# ── Search API (TODO §5e.11) ──────────────────────────────────────────────
# Rich-filter list, tool-call cross-run search, gene catalogue, aggregates.
# Storage abstraction lives in tracing/server/store.py — same shape will
# accept a future FilesystemTraceStore for the secondary FS-only viewer.

from tracing.server.store import SQLiteTraceStore, RunFilter, ToolCallFilter


def _store() -> SQLiteTraceStore:
    return SQLiteTraceStore()


def _split_csv(v: Optional[str]) -> list[str]:
    if not v:
        return []
    return [s.strip() for s in v.split(",") if s.strip()]


@app.get("/api/search/runs")
async def api_search_runs(
    # run attributes
    kind: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    duration_gt_ms: Optional[int] = None,
    tokens_gt: Optional[int] = None,
    # evolutionary identity
    generation: Optional[str] = None,
    mutation: Optional[str] = None,
    gene: Optional[str] = None,            # CSV: all listed must be present (AND)
    gene_any: Optional[str] = None,        # CSV: any of these (OR)
    without_gene: Optional[str] = None,    # CSV: NOT contains
    # work object
    pr_url: Optional[str] = None,
    project: Optional[str] = None,
    file: Optional[str] = None,
    jira: Optional[str] = None,
    scenario: Optional[str] = None,
    scenario_tag: Optional[str] = None,
    # relationship
    linked_run: Optional[str] = None,
    # pagination & sort
    limit: int = 50,
    offset: int = 0,
    sort: str = "started_at",
    order: str = "desc",
):
    """Run search with the §5e.11 filters. All params optional.

    CSV params (`gene`, `gene_any`, `without_gene`) accept comma-
    separated lists. Returns `{data, meta}` envelope so the
    `--json` CLI mode (TODO §5e.13) and the agent-friendly stdout
    contract are stable.
    """
    f = RunFilter(
        kind=kind, agent_name=agent, model=model, status=status,
        since=since, until=until,
        duration_gt_ms=duration_gt_ms, tokens_gt=tokens_gt,
        generation=generation, mutation=mutation,
        genes=_split_csv(gene),
        genes_any=_split_csv(gene_any),
        without_gene=_split_csv(without_gene),
        pr_url=pr_url, project=project, file=file, jira=jira,
        scenario_id=scenario, scenario_tag=scenario_tag,
        linked_run=linked_run,
        limit=max(1, min(500, int(limit))),
        offset=max(0, int(offset)),
        sort=sort, order=order,
    )
    s = _store()
    runs = s.list_runs(f)
    total = s.count_runs(f)
    return JSONResponse({
        "data": runs,
        "meta": {
            "total": total,
            "limit": f.limit, "offset": f.offset,
            "has_more": (f.offset + len(runs)) < total,
        },
    })


@app.get("/api/search/tool_calls")
async def api_search_tool_calls(
    tool: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    args_contains: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Find tool-call examples across all runs (§5e.11 layer 3).

    Result rows include enough run context (model, agent, scenario,
    fs_trace_path) that the consumer can render or drill in without
    a second round-trip.
    """
    f = ToolCallFilter(
        tool=tool, agent_name=agent, model=model,
        args_contains=args_contains,
        since=since, until=until,
        limit=max(1, min(500, int(limit))),
        offset=max(0, int(offset)),
    )
    hits = _store().search_tool_calls(f)
    return JSONResponse({
        "data": hits,
        "meta": {"limit": f.limit, "offset": f.offset, "returned": len(hits)},
    })


@app.get("/api/search/genes")
async def api_search_genes():
    """Gene catalogue: every gene observed in any run + its run count."""
    return JSONResponse({"data": _store().list_genes()})


@app.get("/api/search/aggregates/by_provider")
async def api_aggregate_by_provider(
    kind: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    f = RunFilter(kind=kind, since=since, until=until,
                  limit=10**9, offset=0)
    return JSONResponse({"data": _store().aggregate_by_provider(f)})


@app.get("/api/search/aggregates/by_scenario")
async def api_aggregate_by_scenario(
    since: Optional[str] = None,
    until: Optional[str] = None,
    generation: Optional[str] = None,
):
    f = RunFilter(since=since, until=until, generation=generation,
                  limit=10**9, offset=0)
    return JSONResponse({"data": _store().aggregate_by_scenario(f)})


@app.get("/api/search/aggregates/by_gene")
async def api_aggregate_by_gene(
    scenario_tag: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    f = RunFilter(scenario_tag=scenario_tag, since=since, until=until,
                  limit=10**9, offset=0)
    return JSONResponse({"data": _store().aggregate_by_gene(f)})


@app.get("/api/search/runs/{run_id}")
async def api_search_run(run_id: str):
    r = _store().get_run(run_id)
    if not r:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"run {run_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": r})


# ── Existing endpoints continue below ──────────────────────────────────────

@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str):
    """All events for a run as a flat array (for initial bulk load)."""
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, agent_id, agent_name, event_type, step, data_json, timestamp "
        "FROM events WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    conn.close()
    events = []
    for ev in rows:
        data = json.loads(ev["data_json"]) if ev["data_json"] else {}
        events.append({
            "id": ev["id"],
            "agent_id": ev["agent_id"] or "",
            "agent_name": ev["agent_name"] or "",
            "event_type": ev["event_type"],
            "step": ev["step"],
            "timestamp": ev["timestamp"],
            "data": data,
        })
    return JSONResponse(content=events)


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/live/{run_id}")
async def ws_live(websocket: WebSocket, run_id: str, after: int = 0):
    """Push new events to browser in real-time."""
    await websocket.accept()
    last_event_id = after

    try:
        while True:
            conn = sqlite3.connect(str(DEFAULT_DB_PATH))
            conn.row_factory = sqlite3.Row

            new_events = conn.execute(
                "SELECT id, agent_id, agent_name, event_type, step, data_json, timestamp "
                "FROM events WHERE run_id = ? AND id > ? ORDER BY id LIMIT 50",
                (run_id, last_event_id),
            ).fetchall()

            run_row = conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            run_status = run_row["status"] if run_row else "unknown"
            conn.close()

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
