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


@app.get("/api/search/dimensions")
async def api_search_dimensions():
    """Distinct values per filter dimension — UI dropdown source.
    One call replaces N catalogue calls on the runs page load.
    """
    return JSONResponse({"data": _store().list_dimensions()})


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


@app.get("/api/search/compare")
async def api_compare_mutations(a: str, b: str):
    """Side-by-side compare of two mutation hashes — per (scenario,
    provider) runs / avg-duration / completed counts. Used by
    /qa/compare?a=…&b=…
    """
    return JSONResponse({"data": _store().compare_mutations(a, b)})


@app.get("/api/search/aggregates/by_mutation")
async def api_aggregate_by_mutation(
    generation: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    f = RunFilter(generation=generation, since=since, until=until,
                  limit=10**9, offset=0)
    return JSONResponse({"data": _store().aggregate_by_mutation(f)})


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
from quality_api.discovery import AutoPlanStore, config_to_dict


_qa_queue = TaskQueue()
_qa_plans = PlanStore(_qa_queue)
_qa_auto = AutoPlanStore(_qa_queue)
# Reap stale leases on server start so kill -9 mid-run doesn't strand tasks.
_qa_queue.reap_stale_leases(grace_seconds=0)


class TaskCreatePayload(BaseModel):
    scenario_id: str
    provider: str
    attempt_n: int = 1
    branch: str = ""
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
        attempt_n=p.attempt_n, branch=p.branch,
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
    return JSONResponse({"data": _qa_queue.list_workers()})


# ── Plans (group of tasks via cross-product) ───────────────────────────────

class PlanCreatePayload(BaseModel):
    name: str = ""
    created_by: str = ""
    branches: list[str] = []                 # [] = single empty-branch row
    providers: list[str]
    scenarios: list[str]
    attempts_min: int = 1
    priority: int = 100
    notes: str = ""


@app.post("/api/qa/plans")
async def api_qa_create_plan(p: PlanCreatePayload):
    """Cross-product (branches × providers × scenarios × attempts_min)
    of tasks, all tagged with the new plan_id. Empty branches → one row
    per (provider, scenario) — for scenarios that don't carry a branch.
    """
    try:
        plan_id, task_ids = _qa_plans.create(PlanSpec(
            name=p.name, created_by=p.created_by,
            branches=p.branches, providers=p.providers,
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
    rows = _qa_plans.list(state=state,
                          limit=max(1, min(500, limit)),
                          offset=max(0, offset))
    return JSONResponse({
        "data": [plan_to_dict(p,
                              progress=_qa_plans.progress(p.id))
                 for p in rows],
        "meta": {"limit": limit, "offset": offset, "returned": len(rows)},
    })


# ── HTML pages for the search/QA dimensions ─────────────────────────────────

@app.get("/qa/", response_class=HTMLResponse)
async def qa_dashboard(request: Request):
    return templates.TemplateResponse(request, "qa_dashboard.html", {})


@app.get("/qa/runs", response_class=HTMLResponse)
async def qa_runs_page(request: Request):
    return templates.TemplateResponse(request, "qa_runs.html", {})


@app.get("/qa/plans", response_class=HTMLResponse)
async def qa_plans_page(request: Request):
    return templates.TemplateResponse(request, "qa_plans.html", {})


@app.get("/qa/genes", response_class=HTMLResponse)
async def qa_genes_page(request: Request):
    return templates.TemplateResponse(request, "qa_genes.html", {})


@app.get("/qa/auto-plan", response_class=HTMLResponse)
async def qa_auto_plan_page(request: Request):
    return templates.TemplateResponse(request, "qa_auto_plan.html", {})


@app.get("/qa/mutations", response_class=HTMLResponse)
async def qa_mutations_page(request: Request):
    return templates.TemplateResponse(request, "qa_mutations.html", {})


@app.get("/api/qa/plans/{plan_id}")
async def api_qa_get_plan(plan_id: int):
    p = _qa_plans.get(plan_id)
    if not p:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"plan {plan_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": plan_to_dict(p,
                                              progress=_qa_plans.progress(plan_id))})


@app.post("/api/qa/plans/{plan_id}/cancel")
async def api_qa_cancel_plan(plan_id: int):
    n = _qa_plans.cancel(plan_id)
    return JSONResponse({"data": {"cancelled_tasks": n}})


# ── Auto-plan (5c discover→plan loop, git-driven) ──────────────────────────

class AutoPlanCreatePayload(BaseModel):
    name: str = ""
    repo_path: str
    branch_pattern: str
    providers: list[str]
    unit_scenarios: list[str]
    full_scenarios: list[str] = []
    full_period_seconds: int = 86400
    attempts_min: int = 1
    enabled: bool = True


@app.post("/api/qa/auto-plan/configs")
async def api_qa_auto_create(p: AutoPlanCreatePayload):
    try:
        cid = _qa_auto.add_config(
            name=p.name, repo_path=p.repo_path,
            branch_pattern=p.branch_pattern,
            providers=p.providers,
            unit_scenarios=p.unit_scenarios,
            full_scenarios=p.full_scenarios,
            full_period_seconds=p.full_period_seconds,
            attempts_min=p.attempts_min,
            enabled=p.enabled,
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
    if enabled is None:
        return JSONResponse({"error": {"code": "no_op", "message": "nothing to update"}},
                             status_code=400)
    ok = _qa_auto.set_enabled(config_id, enabled)
    if not ok:
        return JSONResponse({"error": {"code": "not_found",
                                       "message": f"config {config_id} not found"}},
                             status_code=404)
    return JSONResponse({"data": {"ok": True}})


@app.delete("/api/qa/auto-plan/configs/{config_id}")
async def api_qa_auto_delete(config_id: int):
    ok = _qa_auto.delete_config(config_id)
    return JSONResponse({"data": {"ok": ok}})


@app.get("/api/qa/promote-ready")
async def api_qa_promote_ready():
    """Most recent promote-ready full plan per (config, branch).

    A plan is promote_ready=true when state='done' and every task
    finished cleanly (no errors). For auto-plans the kind comes from
    qa_planned_commits.plan_kind — only 'full' kind plans gate promote.
    """
    import sqlite3
    from orchestra.trace_db import DEFAULT_DB_PATH
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT pc.config_id, pc.branch, pc.sha, pc.planned_at,
               p.id AS plan_id, p.state, p.promote_ready, p.created_at
        FROM qa_planned_commits pc
        JOIN qa_plans p ON p.id = pc.plan_id
        WHERE pc.plan_kind='full' AND p.state='done' AND p.promote_ready=1
        ORDER BY pc.config_id, pc.branch, pc.planned_at DESC
    """).fetchall()
    conn.close()
    # Keep only the most recent promote-ready per (config, branch).
    seen: set[tuple] = set()
    out = []
    for r in rows:
        key = (r["config_id"], r["branch"])
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    return JSONResponse({"data": out})


@app.post("/api/qa/auto-plan/discover")
async def api_qa_auto_discover(config_id: Optional[int] = None):
    """Manual discover sweep. Idempotent — only creates plans for
    (branch, sha, kind) triples not yet planned. Watch-daemon will
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
