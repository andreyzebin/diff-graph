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


# ── OpenTelemetry-shaped read API (Phase 0) ───────────────────────────────
# Renders our existing runs + events tables as OTLP-JSON without
# touching the writers. Each sub-agent (run_id × agent_id) becomes
# one Span; events become Span events. trace_id and span_id are
# derived deterministically from our internal ids via SHA-256 truncate
# so the same input always maps to the same hex id (16 bytes for
# trace_id, 8 bytes for span_id — per OTLP spec).

def _otel_trace_id(run_id: str) -> str:
    """16-byte hex (32 chars) — OTLP requires fixed length."""
    import hashlib
    return hashlib.sha256(run_id.encode()).hexdigest()[:32]


def _otel_span_id(span_key: str) -> str:
    """8-byte hex (16 chars)."""
    import hashlib
    return hashlib.sha256(span_key.encode()).hexdigest()[:16]


def _to_unix_nanos(iso: str) -> int:
    """ISO8601 (naive or aware) → unix nanoseconds. OTLP wants ints."""
    from datetime import datetime, timezone as _tz
    if not iso:
        return 0
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz.utc)
        return int(d.timestamp() * 1_000_000_000)
    except Exception:
        return 0


def _otlp_attr_list(attrs: dict) -> list:
    """Render an attributes dict as the OTLP `attributes` array."""
    out = []
    for k, v in (attrs or {}).items():
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


@app.get("/api/otel/traces/{run_id}")
async def api_otel_trace(run_id: str):
    """OTLP-JSON projection of one session.

    Reads from the `otel_spans` table populated by the
    SQLiteSpanExporter (Phase 1). Looks up the trace by the
    `diffgraph.run_id` attribute on the cli.session span — that's
    the index key set by cli.py. If no real OTel data exists yet
    (pre-instrumentation session), falls back to the synthesised
    shape derived from `runs` + `events` so old data stays viewable.

    Compatible with anything that ingests OTLP-JSON: pass to a
    Tempo/Jaeger collector via `curl -X POST -d @file.json`, or
    consume programmatically.
    """
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # Real-OTel path: find the cli.session span whose
        # diffgraph.run_id attribute matches; pull its trace_id and
        # return ALL spans of that trace.
        try:
            row = conn.execute(
                """SELECT trace_id FROM otel_spans
                   WHERE name = 'cli.session'
                     AND json_extract(attributes, '$."diffgraph.run_id"') = ?
                   ORDER BY start_ns DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None  # otel_spans table doesn't exist yet
        if row:
            trace_id = row["trace_id"]
            spans_rows = conn.execute(
                """SELECT span_id, parent_span_id, name, kind,
                          service_name, start_ns, end_ns,
                          status_code, attributes
                   FROM otel_spans WHERE trace_id = ?
                   ORDER BY start_ns ASC""",
                (trace_id,),
            ).fetchall()
            spans_by_service: dict = {}
            for s in spans_rows:
                try:
                    attrs = json.loads(s["attributes"] or "{}")
                except Exception:
                    attrs = {}
                rec = {
                    "traceId": trace_id,
                    "spanId": s["span_id"],
                    "name": s["name"],
                    "kind": s["kind"] or 1,
                    "startTimeUnixNano": int(s["start_ns"] or 0),
                    "endTimeUnixNano": int(s["end_ns"] or 0),
                    "attributes": _otlp_attr_list(attrs),
                    "status": {"code": (
                        1 if (s["status_code"] or "OK") in ("OK", "UNSET")
                        else 2)},
                }
                if s["parent_span_id"]:
                    rec["parentSpanId"] = s["parent_span_id"]
                spans_by_service.setdefault(
                    s["service_name"] or "diffgraph", []).append(rec)
            return JSONResponse({
                "resourceSpans": [
                    {
                        "resource": {"attributes": [
                            {"key": "service.name",
                             "value": {"stringValue": svc}},
                        ]},
                        "scopeSpans": [{
                            "scope": {"name": "diffgraph.agents"},
                            "spans": spans,
                        }],
                    }
                    for svc, spans in spans_by_service.items()
                ],
            })

        # Fallback: derive a coarse OTLP shape from runs + events
        # (Phase 0 path). Used only for pre-instrumentation runs.
        run = conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            return JSONResponse({"error": {"code": "not_found",
                                            "message": run_id}},
                                 status_code=404)
        sub_rows = conn.execute(
            """SELECT agent_id, agent_name,
                      MIN(timestamp) AS started_at,
                      MAX(timestamp) AS finished_at,
                      COUNT(*) AS event_count
               FROM events WHERE run_id=? AND agent_id IS NOT NULL
                              AND agent_id != ''
               GROUP BY agent_id, agent_name
               ORDER BY MIN(timestamp)""",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    trace_id = _otel_trace_id(run_id)
    root_span_id = _otel_span_id(run_id)

    spans = []
    # Root span — the session itself (one CLI invocation / webhook
    # request / etc.). Wraps all sub-agent spans.
    spans.append({
        "traceId": trace_id,
        "spanId": root_span_id,
        "name": (run["agent_name"] or run["kind"] or "session"),
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": _to_unix_nanos(run["started_at"]),
        "endTimeUnixNano": _to_unix_nanos(run["finished_at"]),
        "attributes": [
            {"key": "diffgraph.run_id", "value": {"stringValue": run["id"]}},
            {"key": "diffgraph.kind",   "value": {"stringValue": run["kind"] or ""}},
            {"key": "diffgraph.model",  "value": {"stringValue": run["model"] or ""}},
            {"key": "diffgraph.scenario_id",
             "value": {"stringValue": run["scenario_id"] or ""}},
            {"key": "diffgraph.mutation",
             "value": {"stringValue": run["mutation"] or ""}},
            {"key": "diffgraph.generation",
             "value": {"stringValue": run["generation"] or ""}},
            {"key": "diffgraph.linked_run_id",
             "value": {"stringValue": run["linked_run_id"] or ""}},
        ],
        "status": {"code": 1 if run["status"] == "completed" else 2},
    })
    for r in sub_rows:
        agent_span_key = f"{run_id}:{r['agent_id']}:{r['agent_name']}"
        spans.append({
            "traceId": trace_id,
            "spanId": _otel_span_id(agent_span_key),
            "parentSpanId": root_span_id,
            "name": r["agent_name"] or "agent",
            "kind": 1,
            "startTimeUnixNano": _to_unix_nanos(r["started_at"]),
            "endTimeUnixNano": _to_unix_nanos(r["finished_at"]),
            "attributes": [
                {"key": "diffgraph.agent_id",
                 "value": {"stringValue": r["agent_id"]}},
                {"key": "diffgraph.agent_name",
                 "value": {"stringValue": r["agent_name"] or ""}},
                {"key": "diffgraph.event_count",
                 "value": {"intValue": str(r["event_count"])}},
            ],
            "status": {"code": 1},
        })
    return JSONResponse({
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": "diffgraph"}},
                    {"key": "service.version",
                     "value": {"stringValue": "0.1"}},
                ],
            },
            "scopeSpans": [{
                "scope": {"name": "diffgraph.agents", "version": "0.1"},
                "spans": spans,
            }],
        }],
    })


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
# Rich-filter list, tool-call cross-run search, aggregates.
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
    # work object
    pr_url: Optional[str] = None,
    project: Optional[str] = None,
    file: Optional[str] = None,
    jira: Optional[str] = None,
    scenario: Optional[str] = None,
    scenario_tag: Optional[str] = None,
    # relationship
    linked_run: Optional[str] = None,
    # scheduling — show only runs from this plan (joined via qa_tasks)
    plan: Optional[int] = None,
    task: Optional[int] = None,        # narrow further to one task row
    session: Optional[str] = None,     # pin to one runs.id (CLI scope)
    scenario_run: Optional[str] = None,  # pin to one (agent ↔ judge) pair
    # pagination & sort
    limit: int = 50,
    offset: int = 0,
    sort: str = "started_at",
    order: str = "desc",
):
    """Run search with the §5e.11 filters. All params optional.

    Returns `{data, meta}` envelope so the `--json` CLI mode (TODO
    §5e.13) and the agent-friendly stdout contract are stable.
    """
    f = RunFilter(
        agent_name=agent, model=model, status=status,
        since=since, until=until,
        duration_gt_ms=duration_gt_ms, tokens_gt=tokens_gt,
        generation=generation, mutation=mutation,
        pr_url=pr_url, project=project, file=file, jira=jira,
        scenario_id=scenario, scenario_tag=scenario_tag,
        linked_run=linked_run,
        plan_id=plan,
        task_id=task,
        session_id=session,
        scenario_run_id=scenario_run,
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


@app.get("/api/search/sub_runs")
async def api_search_sub_runs(
    # Same filter set as /api/search/runs — applies to the parent
    # runs row; the view then flattens each session into one row
    # per sub-agent (run_id × agent_id × agent_name).
    agent: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    duration_gt_ms: Optional[int] = None,
    tokens_gt: Optional[int] = None,
    generation: Optional[str] = None,
    mutation: Optional[str] = None,
    pr_url: Optional[str] = None,
    project: Optional[str] = None,
    file: Optional[str] = None,
    jira: Optional[str] = None,
    scenario: Optional[str] = None,
    scenario_tag: Optional[str] = None,
    linked_run: Optional[str] = None,
    plan: Optional[int] = None,
    task: Optional[int] = None,
    session: Optional[str] = None,
    scenario_run: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "started_at",
    order: str = "desc",
):
    """Sub-agent view of /api/search/runs — each row is one
    `(run_id, agent_id, agent_name)` triple. Production webhook-driven
    sessions surface here too: their sub-agents (dispatcher → reviewer
    → investigator-N) show up the same way bench-driven sessions do,
    just with plan/task columns NULL since they aren't tied to a
    qa_tasks row."""
    f = RunFilter(
        agent_name=agent, model=model, status=status,
        since=since, until=until,
        duration_gt_ms=duration_gt_ms, tokens_gt=tokens_gt,
        generation=generation, mutation=mutation,
        pr_url=pr_url, project=project, file=file, jira=jira,
        scenario_id=scenario, scenario_tag=scenario_tag,
        linked_run=linked_run,
        plan_id=plan, task_id=task,
        session_id=session, scenario_run_id=scenario_run,
        limit=max(1, min(500, int(limit))),
        offset=max(0, int(offset)),
        sort=sort, order=order,
    )
    s = _store()
    rows = s.list_sub_runs(f)
    total = s.count_sub_runs(f)
    return JSONResponse({
        "data": rows,
        "meta": {
            "total": total,
            "limit": f.limit, "offset": f.offset,
            "has_more": (f.offset + len(rows)) < total,
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


@app.get("/api/search/dimensions")
async def api_search_dimensions():
    """Distinct values per filter dimension — UI dropdown source.
    One call replaces N catalogue calls on the runs page load.
    """
    return JSONResponse({"data": _store().list_dimensions()})


@app.get("/api/search/aggregates/by_provider")
async def api_aggregate_by_provider(
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    f = RunFilter(since=since, until=until, limit=10**9, offset=0)
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


@app.get("/api/search/compare")
async def api_compare_mutations(a: str, b: str):
    """Side-by-side compare of two mutation hashes — per (scenario,
    provider) runs / avg-duration / completed counts. Used by
    /qa/compare?a=…&b=…
    """
    return JSONResponse({"data": _store().compare_mutations(a, b)})


@app.get("/api/search/scoring/{mutation}")
async def api_mutation_scoring(mutation: str):
    """Engineering-assessment-style scoring for one mutation. Returns
    {overall, axes:{hard_skill, soft_skill, methodology}, by_cell,
    by_warning_kind}.
    """
    return JSONResponse({"data": _store().mutation_scoring(mutation)})


@app.get("/api/search/per_run_scores")
async def api_per_run_scores(
    mutation: Optional[str] = None,
    scenario: Optional[str] = None,
    generation: Optional[str] = None,
    lineage: Optional[str] = None,
    limit: int = 1000,
):
    """Flat per-run judge scores for charting (box / violin / timeline).
    Each row is one (agent ↔ judge) pair with overall_score, hard /
    soft / methodology axes, fp_count, warnings_count, found_rate.
    Filters narrow incrementally: pass `lineage` (= a long-lived line
    of git commits we're testing as a hypothesis, typically a git
    branch like `master` or `feature/foo`; tracked in qa_tasks.lineage)
    to watch every commit on it.
    """
    rows = _store().per_run_scores(
        mutation=mutation, scenario=scenario,
        generation=generation, lineage=lineage,
        limit=max(1, min(5000, int(limit))),
    )
    return JSONResponse({"data": rows,
                         "meta": {"returned": len(rows), "limit": limit}})


@app.get("/api/search/scoring-compare")
async def api_scoring_compare(a: str, b: str):
    """Side-by-side scoring of two mutations along assessment axes."""
    return JSONResponse({
        "data": {
            "a": _store().mutation_scoring(a),
            "b": _store().mutation_scoring(b),
        },
    })


@app.get("/api/search/aggregates/by_mutation")
async def api_aggregate_by_mutation(
    generation: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    f = RunFilter(generation=generation, since=since, until=until,
                  limit=10**9, offset=0)
    rows = _store().aggregate_by_mutation(f)
    total = len(rows)
    eff_limit = max(1, min(500, limit))
    eff_offset = max(0, offset)
    page = rows[eff_offset:eff_offset + eff_limit]
    return JSONResponse({
        "data": page,
        "meta": {"limit": eff_limit, "offset": eff_offset,
                 "returned": len(page), "total": total},
    })


@app.get("/api/search/runs/{run_id}")
async def api_search_run(run_id: str):
    r = _store().get_run(run_id)
    if not r:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"run {run_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": r})


# ── Task queue API (TODO §5e.4 / §5e.9 step 3) ─────────────────────────────
# Worker contract: enqueue / lease / heartbeat / finish + reaper.
# Plans (group of tasks) and discover (git-fetch driven enqueue) sit on top
# of these primitives in a follow-up.

from pydantic import BaseModel
from typing import Any
from quality_api.queue import (
    TaskQueue, TaskSpec, task_to_dict,
    PlanStore, PlanSpec, plan_to_dict,
)
from quality_api.discovery import AutoPlanStore, DiscoverySupervisor, config_to_dict
from quality_api.pools import PoolStore, WorkerSupervisor, pool_to_dict


_qa_queue = TaskQueue()
_qa_plans = PlanStore(_qa_queue)
_qa_auto = AutoPlanStore(_qa_queue)
_qa_pools = PoolStore(_qa_queue)
_qa_supervisor = WorkerSupervisor(_qa_queue, _qa_pools, check_interval_seconds=20)
_qa_discovery = DiscoverySupervisor(_qa_auto, interval_seconds=60)
# Reap stale leases on server start so kill -9 mid-run doesn't strand tasks.
_qa_queue.reap_stale_leases(grace_seconds=0)


@app.on_event("startup")
async def _start_qa_supervisors():
    _qa_supervisor.start()
    _qa_discovery.start()


@app.on_event("shutdown")
async def _stop_qa_supervisors():
    await _qa_supervisor.stop()
    await _qa_discovery.stop()


class TaskCreatePayload(BaseModel):
    scenario_id: str
    provider: str
    attempt_n: int = 1
    lineage: str = ""
    mutation_hash: str = ""
    plan_id: Optional[int] = None
    priority: int = 100
    payload: dict = {}


class TaskFinishPayload(BaseModel):
    worker_id: str
    state: str = "finished"           # finished | error | cancelled
    trace_run_id: Optional[str] = None
    result: Optional[dict] = None
    error_class: Optional[str] = None


class TaskHeartbeatPayload(BaseModel):
    worker_id: str
    lease_seconds: int = 60


class TaskLeasePayload(BaseModel):
    provider: str
    worker_id: str
    lease_seconds: int = 60


class WorkerRegisterPayload(BaseModel):
    worker_id: Optional[str] = None
    provider: str = ""
    capacity: int = 1
    pid: Optional[int] = None


@app.post("/api/qa/tasks")
async def api_qa_create_task(p: TaskCreatePayload):
    """Enqueue a single task. Future /api/qa/plans will fan out into
    many of these in one request."""
    spec = TaskSpec(
        scenario_id=p.scenario_id, provider=p.provider,
        attempt_n=p.attempt_n, lineage=p.lineage,
        mutation_hash=p.mutation_hash, plan_id=p.plan_id,
        priority=p.priority, payload=p.payload or {},
    )
    task_id = _qa_queue.enqueue(spec)
    t = _qa_queue.get(task_id)
    return JSONResponse({"data": task_to_dict(t)}, status_code=201)


@app.get("/api/qa/tasks")
async def api_qa_list_tasks(
    state: Optional[str] = None,
    provider: Optional[str] = None,
    plan_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    rows = _qa_queue.list(state=state, provider=provider, plan_id=plan_id,
                          limit=max(1, min(500, limit)),
                          offset=max(0, offset))
    return JSONResponse({
        "data": [task_to_dict(r) for r in rows],
        "meta": {"limit": limit, "offset": offset, "returned": len(rows)},
    })


@app.get("/api/qa/tasks/{task_id}")
async def api_qa_get_task(task_id: int):
    t = _qa_queue.get(task_id)
    if not t:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"task {task_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": task_to_dict(t)})


@app.post("/api/qa/tasks/lease")
async def api_qa_lease(p: TaskLeasePayload):
    """Atomic next-task pickup. Returns 204 if queue is empty for
    this provider — cleaner than 200 with null data for poll loops."""
    t = _qa_queue.lease(provider=p.provider, worker_id=p.worker_id,
                        lease_seconds=max(5, p.lease_seconds))
    if t is None:
        # 200 with data=null — workers poll this in a loop, easier to
        # branch on `data is None` than to check for 204.
        return JSONResponse({"data": None, "meta": {"empty": True}})
    return JSONResponse({"data": task_to_dict(t)})


@app.post("/api/qa/tasks/{task_id}/heartbeat")
async def api_qa_heartbeat(task_id: int, p: TaskHeartbeatPayload):
    """Extend lease. Returns 200 with `{ok: false}` if the task was
    reaped or finished — worker should stop work then."""
    ok = _qa_queue.heartbeat(task_id, worker_id=p.worker_id,
                             lease_seconds=max(5, p.lease_seconds))
    return JSONResponse({"data": {"ok": ok}})


@app.post("/api/qa/tasks/{task_id}/finish")
async def api_qa_finish(task_id: int, p: TaskFinishPayload):
    if p.state not in ("finished", "error", "cancelled"):
        return JSONResponse({"error": {"code": "invalid_state",
                                       "message": f"state must be finished|error|cancelled, got {p.state}"}},
                             status_code=400)
    ok = _qa_queue.finish(task_id, worker_id=p.worker_id, state=p.state,
                          trace_run_id=p.trace_run_id, result=p.result,
                          error_class=p.error_class)
    if not ok:
        return JSONResponse({"error": {"code": "lease_lost",
                                       "message": f"task {task_id}: lease lost or wrong worker"}},
                             status_code=409)
    return JSONResponse({"data": {"ok": True}})


@app.post("/api/qa/tasks/{task_id}/cancel")
async def api_qa_cancel(task_id: int):
    ok = _qa_queue.cancel(task_id)
    if not ok:
        return JSONResponse({"error": {"code": "terminal",
                                       "message": f"task {task_id}: already in terminal state"}},
                             status_code=409)
    return JSONResponse({"data": {"ok": True}})


@app.post("/api/qa/tasks/reap")
async def api_qa_reap(grace_seconds: int = 30):
    """Manually trigger the reaper (also runs on server start)."""
    n = _qa_queue.reap_stale_leases(grace_seconds=grace_seconds)
    return JSONResponse({"data": {"reaped": n}})


@app.post("/api/qa/workers")
async def api_qa_register_worker(p: WorkerRegisterPayload):
    wid = _qa_queue.register_worker(
        worker_id=p.worker_id, provider=p.provider,
        capacity=p.capacity, pid=p.pid,
    )
    return JSONResponse({"data": {"worker_id": wid}}, status_code=201)


@app.post("/api/qa/workers/{worker_id}/heartbeat")
async def api_qa_worker_heartbeat(worker_id: str):
    ok = _qa_queue.worker_heartbeat(worker_id)
    return JSONResponse({"data": {"ok": ok}})


@app.get("/api/qa/workers")
async def api_qa_list_workers():
    """Worker fleet + each worker's currently-leased task.

    Self-healing happens inside TaskQueue.list_workers (called below):
      - Workers with stale heartbeat (>90s) AND state='running'
        get auto-flipped to state='dead'.
      - Worker rows state ∈ ('dead','stopped') older than 1h are
        auto-deleted.
    UI polls every 5s, fleet stays tidy without manual cleanup.
    """
    import sqlite3
    from datetime import datetime, timezone
    from orchestra.trace_db import DEFAULT_DB_PATH
    workers = _qa_queue.list_workers()
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, scenario_id, provider, plan_id, state, lease_owner,
               started_at, lease_expires_at
        FROM qa_tasks
        WHERE state IN ('leased', 'running') AND lease_owner != ''
    """).fetchall()
    conn.close()
    by_owner: dict[str, dict] = {}
    for r in rows:
        by_owner[r["lease_owner"]] = dict(r)
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.now()
    out = []
    for w in workers:
        d = dict(w)
        try:
            last = datetime.fromisoformat(w["last_heartbeat"])
            # Old rows (pre UTC migration) are naive local time; new
            # rows are tz-aware UTC. Compare against the matching now
            # so we don't get a TypeError that hides liveness.
            ref = now_utc if last.tzinfo else now_naive
            d["heartbeat_age_s"] = int((ref - last).total_seconds())
        except Exception:
            d["heartbeat_age_s"] = None
        # Health classification (used by the fleet UI):
        #   running  — fresh heartbeat (<90s) and worker_state == 'running'
        #   stopped  — clean exit, worker_state == 'stopped'
        #   dead     — stale heartbeat AND state != 'stopped' (crashed
        #              or forcibly killed)
        recorded = (w.get("state") or "running").strip().lower()
        fresh_hb = (d["heartbeat_age_s"] is not None
                    and d["heartbeat_age_s"] < 90)
        if recorded == "stopped":
            health = "stopped"
        elif fresh_hb and recorded == "running":
            health = "running"
        else:
            health = "dead"
        d["health"] = health
        # Back-compat for the existing UI: alive == running or stopped
        # (the worker hasn't crashed). Busy/idle dot derives from this.
        d["alive"] = (health == "running")
        d["current_task"] = by_owner.get(w["id"])
        out.append(d)
    return JSONResponse({"data": out})


# ── Worker pools (server-side supervisor) ─────────────────────────────────

class PoolCreatePayload(BaseModel):
    name: str = ""
    provider: str
    target_workers: int = 1
    trigger: str = "live_queue"
    max_idle_seconds: int = 120
    task_timeout_seconds: int = 900
    bench_cmd: str = ""
    enabled: bool = True


class PoolUpdatePayload(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    target_workers: Optional[int] = None
    trigger: Optional[str] = None
    max_idle_seconds: Optional[int] = None
    task_timeout_seconds: Optional[int] = None
    bench_cmd: Optional[str] = None
    enabled: Optional[bool] = None


@app.post("/api/qa/worker-pools")
async def api_qa_pool_create(p: PoolCreatePayload):
    try:
        pid = _qa_pools.add(
            name=p.name, provider=p.provider,
            target_workers=p.target_workers, trigger=p.trigger,
            max_idle_seconds=p.max_idle_seconds,
            task_timeout_seconds=p.task_timeout_seconds,
            bench_cmd=p.bench_cmd,
            enabled=p.enabled,
        )
    except ValueError as e:
        return JSONResponse({"error": {"code": "invalid", "message": str(e)}},
                             status_code=400)
    return JSONResponse({"data": pool_to_dict(_qa_pools.get(pid))}, status_code=201)


@app.get("/api/qa/worker-pools")
async def api_qa_pool_list():
    return JSONResponse({"data": [pool_to_dict(p) for p in _qa_pools.list()]})


@app.put("/api/qa/worker-pools/{pool_id}")
async def api_qa_pool_update(pool_id: int, p: PoolUpdatePayload):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        return JSONResponse({"error": {"code": "no_op", "message": "nothing to update"}},
                             status_code=400)
    ok = _qa_pools.update(pool_id, **fields)
    if not ok:
        return JSONResponse({"error": {"code": "not_found", "message": f"pool {pool_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": pool_to_dict(_qa_pools.get(pool_id))})


@app.delete("/api/qa/worker-pools/{pool_id}")
async def api_qa_pool_delete(pool_id: int):
    ok = _qa_pools.delete(pool_id)
    return JSONResponse({"data": {"ok": ok}})


@app.post("/api/qa/workers/cleanup-dead")
async def api_qa_cleanup_dead_workers():
    """Remove dead/stopped worker rows older than 1h. Manual button on
    the UI calls this to keep the fleet table tidy."""
    import sqlite3
    from orchestra.trace_db import DEFAULT_DB_PATH
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    try:
        cur = conn.execute(
            """DELETE FROM qa_workers
               WHERE state IN ('dead', 'stopped')
                  OR (state='running' AND last_heartbeat < ?)""",
            (cutoff,),
        )
        conn.commit()
        return JSONResponse({"data": {"deleted": cur.rowcount}})
    finally:
        conn.close()


# ── Plans (group of tasks via cross-product) ───────────────────────────────

class PlanCreatePayload(BaseModel):
    name: str = ""
    created_by: str = ""
    lineages: list[str] = []                 # [] = single empty-lineage row
    providers: list[str]
    scenarios: list[str]
    attempts_min: int = 1
    priority: int = 100
    notes: str = ""


@app.post("/api/qa/plans")
async def api_qa_create_plan(p: PlanCreatePayload):
    """Cross-product (lineages × providers × scenarios × attempts_min)
    of tasks, all tagged with the new plan_id. Empty lineages → one row
    per (provider, scenario) — for scenarios that don't carry a lineage.
    """
    try:
        plan_id, task_ids = _qa_plans.create(PlanSpec(
            name=p.name, created_by=p.created_by,
            lineages=p.lineages, providers=p.providers,
            scenarios=p.scenarios, attempts_min=p.attempts_min,
            priority=p.priority, notes=p.notes,
        ))
    except ValueError as e:
        return JSONResponse({"error": {"code": "invalid_plan",
                                       "message": str(e)}},
                             status_code=400)
    plan = _qa_plans.get(plan_id)
    return JSONResponse({"data": plan_to_dict(plan,
                                              progress=_qa_plans.progress(plan_id)),
                         "meta": {"task_ids": task_ids}},
                        status_code=201)


@app.get("/api/qa/plans")
async def api_qa_list_plans(state: Optional[str] = None,
                            limit: int = 50, offset: int = 0):
    eff_limit = max(1, min(500, limit))
    eff_offset = max(0, offset)
    rows = _qa_plans.list(state=state, limit=eff_limit, offset=eff_offset)
    total = _qa_plans.count(state=state)
    return JSONResponse({
        "data": [plan_to_dict(p,
                              progress=_qa_plans.progress(p.id),
                              eta=_qa_plans.eta(p.id),
                              live_score=_qa_plans.live_score(p.id))
                 for p in rows],
        "meta": {"limit": eff_limit, "offset": eff_offset,
                 "returned": len(rows), "total": total},
    })


# ── HTML pages for the search/QA dimensions ─────────────────────────────────

@app.get("/qa/", response_class=HTMLResponse)
async def qa_dashboard(request: Request):
    return templates.TemplateResponse(request, "qa_dashboard.html", {"active": "overview"})


@app.get("/qa/traces", response_class=HTMLResponse)
async def qa_traces_page(request: Request):
    return templates.TemplateResponse(request, "qa_traces.html", {"active": "traces"})


@app.get("/qa/plans", response_class=HTMLResponse)
async def qa_plans_page(request: Request):
    return templates.TemplateResponse(request, "qa_plans.html", {"active": "plans"})


@app.get("/qa/auto-plan", response_class=HTMLResponse)
async def qa_auto_plan_page(request: Request):
    # `active="schedules"` matches the renamed nav tab; URL stays as
    # /qa/auto-plan for back-compat (rename when more trigger types
    # land beyond git-event).
    return templates.TemplateResponse(request, "qa_auto_plan.html", {"active": "schedules"})


@app.get("/qa/workers", response_class=HTMLResponse)
async def qa_workers_page(request: Request):
    return templates.TemplateResponse(request, "qa_workers.html", {"active": "workers"})


@app.get("/qa/scoring", response_class=HTMLResponse)
async def qa_scoring_page(request: Request):
    return templates.TemplateResponse(request, "qa_scoring.html",
                                       {"active": "scoring"})


@app.get("/qa/mutations", response_class=HTMLResponse)
async def qa_mutations_page(request: Request):
    return templates.TemplateResponse(request, "qa_mutations.html", {"active": "mutations"})


@app.get("/api/qa/plans/{plan_id}")
async def api_qa_get_plan(plan_id: int):
    p = _qa_plans.get(plan_id)
    if not p:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"plan {plan_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": plan_to_dict(p,
                                              progress=_qa_plans.progress(plan_id),
                                              eta=_qa_plans.eta(plan_id),
                                              live_score=_qa_plans.live_score(plan_id))})


@app.post("/api/qa/plans/{plan_id}/cancel")
async def api_qa_cancel_plan(plan_id: int):
    n = _qa_plans.cancel(plan_id)
    return JSONResponse({"data": {"cancelled_tasks": n}})


# ── Auto-plan (5c discover→plan loop, git-driven) ──────────────────────────

class AutoPlanCreatePayload(BaseModel):
    name: str = ""
    repo_path: str
    branch_pattern: str
    bench_repo_path: str = ""
    providers: list[str]
    scenarios: list[str] = []          # explicit ids
    scenario_tags: list[str] = []      # tag filter (resolved at discover-time)
    min_gap_seconds: int = 0           # debounce per lineage (0 = every commit)
    pacing: str = "aggressive"         # 'aggressive' | 'spread'
    pacing_window_seconds: int = 0
    attempts_min: int = 1
    enabled: bool = True
    mode: str = "auto"                 # 'auto' | 'on_demand'


class AutoPlanUpdatePayload(BaseModel):
    # All fields optional — PATCH semantics. None = leave alone.
    name: Optional[str] = None
    repo_path: Optional[str] = None
    branch_pattern: Optional[str] = None
    bench_repo_path: Optional[str] = None
    providers: Optional[list[str]] = None
    scenarios: Optional[list[str]] = None
    scenario_tags: Optional[list[str]] = None
    min_gap_seconds: Optional[int] = None
    pacing: Optional[str] = None
    pacing_window_seconds: Optional[int] = None
    attempts_min: Optional[int] = None
    enabled: Optional[bool] = None
    mode: Optional[str] = None


class AutoPlanFireOnPayload(BaseModel):
    lineage: str
    sha: str


@app.post("/api/qa/auto-plan/configs")
async def api_qa_auto_create(p: AutoPlanCreatePayload):
    try:
        cid = _qa_auto.add_config(
            name=p.name, repo_path=p.repo_path,
            branch_pattern=p.branch_pattern,
            bench_repo_path=p.bench_repo_path,
            providers=p.providers,
            scenarios=p.scenarios,
            scenario_tags=p.scenario_tags,
            min_gap_seconds=p.min_gap_seconds,
            pacing=p.pacing,
            pacing_window_seconds=p.pacing_window_seconds,
            attempts_min=p.attempts_min,
            enabled=p.enabled,
            mode=p.mode,
        )
    except ValueError as e:
        return JSONResponse({"error": {"code": "invalid", "message": str(e)}},
                             status_code=400)
    cfg = _qa_auto.get_config(cid)
    return JSONResponse({"data": config_to_dict(cfg)}, status_code=201)


@app.get("/api/qa/auto-plan/configs")
async def api_qa_auto_list():
    return JSONResponse({"data": [config_to_dict(c) for c in _qa_auto.list_configs()]})


@app.get("/api/qa/auto-plan/configs/{config_id}")
async def api_qa_auto_get(config_id: int):
    c = _qa_auto.get_config(config_id)
    if not c:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"config {config_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": config_to_dict(c)})


@app.patch("/api/qa/auto-plan/configs/{config_id}")
async def api_qa_auto_patch(config_id: int, enabled: Optional[bool] = None):
    """Quick toggle endpoint — enable/disable via query param."""
    if enabled is None:
        return JSONResponse({"error": {"code": "no_op", "message": "nothing to update"}},
                             status_code=400)
    ok = _qa_auto.set_enabled(config_id, enabled)
    if not ok:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"config {config_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": {"ok": True}})


@app.put("/api/qa/auto-plan/configs/{config_id}")
async def api_qa_auto_update(config_id: int, p: AutoPlanUpdatePayload):
    """Full edit. Pass any subset of fields; missing ones stay untouched."""
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        return JSONResponse({"error": {"code": "no_op",
                                       "message": "nothing to update"}},
                             status_code=400)
    ok = _qa_auto.update_config(config_id, **fields)
    if not ok:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"config {config_id} not found"}},
                             status_code=404)
    cfg = _qa_auto.get_config(config_id)
    return JSONResponse({"data": config_to_dict(cfg)})


@app.get("/api/qa/auto-plan/configs/{config_id}/history")
async def api_qa_auto_history(config_id: int, limit: int = 50):
    """Plans created by this config — every (lineage × sha) ever
    planned, joined with qa_plans for current state + progress."""
    import sqlite3
    from orchestra.trace_db import DEFAULT_DB_PATH
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT pc.lineage, pc.sha, pc.plan_kind, pc.plan_id, pc.planned_at,
               p.name AS plan_name, p.state AS plan_state,
               p.promote_ready, p.providers, p.scenarios
        FROM qa_planned_commits pc
        LEFT JOIN qa_plans p ON p.id = pc.plan_id
        WHERE pc.config_id = ?
        ORDER BY pc.planned_at DESC
        LIMIT ?
    """, (config_id, max(1, min(500, limit)))).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        # Add progress counts via the existing PlanStore aggregator.
        if d.get("plan_id"):
            try:
                d["progress"] = _qa_plans.progress(int(d["plan_id"]))
            except Exception:
                d["progress"] = {}
        items.append(d)
    conn.close()
    return JSONResponse({"data": items, "meta": {"returned": len(items)}})


@app.get("/api/qa/auto-plan/configs/{config_id}/preview-scenarios")
async def api_qa_auto_preview_scenarios(config_id: int):
    """Resolve scenario_tags filter against the configured bench repo
    right now. UI uses this to show "filter currently matches N
    scenarios: [...]" before saving / discovering."""
    cfg = _qa_auto.get_config(config_id)
    if not cfg:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"config {config_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": _qa_auto.resolve_config_scenarios(config_id)})


@app.get("/api/qa/scenarios-catalog")
async def api_qa_scenarios_catalog(bench_repo_path: str):
    """List all scenarios in a bench repo with their tags.
    Pure helper for the UI's tag-filter form — query string passes
    the bench path so the UI doesn't need to know it ahead of time.
    """
    from quality_api.discovery import scan_scenarios
    return JSONResponse({"data": scan_scenarios(bench_repo_path)})


@app.delete("/api/qa/auto-plan/configs/{config_id}")
async def api_qa_auto_delete(config_id: int):
    ok = _qa_auto.delete_config(config_id)
    return JSONResponse({"data": {"ok": ok}})


@app.post("/api/qa/auto-plan/configs/{config_id}/fire-on")
async def api_qa_auto_fire_on(config_id: int, p: AutoPlanFireOnPayload):
    """Manually trigger an on_demand schedule on a specific (lineage, sha).

    Use case: 'this commit looks promising on unit-tier — run the full
    integration matrix against it'. The schedule defines WHAT (scenarios,
    providers, attempts_min, pacing); fire-on picks THE COMMIT to run
    it on. Idempotent — same triple won't double-plan.
    """
    out = _qa_auto.fire_on(config_id, lineage=p.lineage, sha=p.sha)
    if out is None:
        return JSONResponse({"error": {"code": "no_op",
                                       "message": "config disabled, no scenarios resolved, or already planned for this commit"}},
                             status_code=409)
    return JSONResponse({"data": out}, status_code=201)


@app.post("/api/qa/auto-plan/discover")
async def api_qa_auto_discover(config_id: Optional[int] = None):
    """Manual discover sweep. Idempotent — only creates plans for
    (lineage, sha, kind) triples not yet planned. Watch-daemon will
    call this on a timer, but it's also safe to invoke from the UI
    button or CLI for ad-hoc sweeps."""
    created = _qa_auto.discover(config_id=config_id)
    return JSONResponse({"data": created,
                         "meta": {"created_count": len(created)}})


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
