"""quality-cli entry point — typer app with two output modes."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

# Local-store mode (offline-first). HTTP mode is a future drop-in.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracing.server.store import RunFilter, SQLiteTraceStore, ToolCallFilter


app = typer.Typer(help="Search and drill over the diff-graph trace DB.",
                  no_args_is_help=True, add_completion=False)
runs_app = typer.Typer(help="Run-level queries.", no_args_is_help=True)
traces_app = typer.Typer(
    help="Trace navigation for AI debug agents — sessions, sub-agent "
         "trees, raw events, and a find-problems probe over the trace DB.",
    no_args_is_help=True,
)
tools_app = typer.Typer(help="Cross-run tool-call search.", no_args_is_help=True)
agg_app = typer.Typer(help="Aggregate views.", no_args_is_help=True)
plans_app = typer.Typer(help="QA plans (group of tasks).", no_args_is_help=True)
tasks_app = typer.Typer(help="QA tasks.", no_args_is_help=True)
auto_app = typer.Typer(help="Auto-plan: git-driven discovery + plan creation.",
                       no_args_is_help=True)
app.add_typer(runs_app, name="runs")
app.add_typer(traces_app, name="traces")
app.add_typer(tools_app, name="tool-calls")
app.add_typer(agg_app, name="aggregates")
app.add_typer(plans_app, name="plans")
app.add_typer(tasks_app, name="tasks")
app.add_typer(auto_app, name="auto-plan")


# Output mode is a global state cell so subcommands can read it without
# threading flags through every signature. Set in main_callback.
class _Out:
    json_mode: bool = False
    quiet: bool = False
    console: Console = Console()


def _emit(data, meta: Optional[dict] = None, suggestion: str = ""):
    """Single output funnel — JSON envelope or human view."""
    if _Out.json_mode:
        payload = {"data": data}
        if meta is not None:
            payload["meta"] = meta
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return
    # Human path — let the caller render via a Rich primitive.
    if isinstance(data, list) and not data and not _Out.quiet:
        msg = "(no results)"
        if suggestion:
            msg += f"\n[dim]hint: {suggestion}[/dim]"
        _Out.console.print(msg)


def _emit_error(code: str, message: str, exit_code: int = 1):
    if _Out.json_mode:
        sys.stdout.write(json.dumps({"error": {"code": code, "message": message}},
                                    ensure_ascii=False) + "\n")
    else:
        _Out.console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(exit_code)


def _store(db: Optional[str]) -> SQLiteTraceStore:
    return SQLiteTraceStore(db_path=db) if db else SQLiteTraceStore()


def _split_csv(s: Optional[str]) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


@app.callback()
def main_callback(
    json_out: bool = typer.Option(False, "--json", help="Emit a stable JSON envelope; suppress color and prompts. For agent consumers."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress 'no results' hints and other suggestions."),
):
    """Configure global output mode. Has to live on a callback because
    typer doesn't otherwise expose a place for app-wide flags."""
    _Out.json_mode = bool(json_out)
    _Out.quiet = bool(quiet)
    if _Out.json_mode:
        # Disable rich color when piping to an agent.
        _Out.console = Console(force_terminal=False, color_system=None)


# ── runs list ─────────────────────────────────────────────────────────────

@runs_app.command("list")
def runs_list(
    # core
    kind: Optional[str] = typer.Option(None, help="agent | judge | evaluator"),
    agent: Optional[str] = typer.Option(None, help="dispatcher | reviewer | investigator | judge"),
    model: Optional[str] = typer.Option(None, help="LLM model name"),
    status: Optional[str] = typer.Option(None, help="completed | error | running"),
    # evolutionary
    generation: Optional[str] = typer.Option(None),
    mutation: Optional[str] = typer.Option(None, help="exact or short hash"),
    # work objects
    project: Optional[str] = typer.Option(None),
    file: Optional[str] = typer.Option(None, help="path in files_touched"),
    jira: Optional[str] = typer.Option(None, help="jira key, e.g. ORD-234"),
    scenario: Optional[str] = typer.Option(None, help="bench scenario id"),
    scenario_tag: Optional[str] = typer.Option(None),
    pr_url: Optional[str] = typer.Option(None),
    # activity
    duration_gt_ms: Optional[int] = typer.Option(None, help="only runs longer than N ms"),
    tokens_gt: Optional[int] = typer.Option(None),
    # range
    since: Optional[str] = typer.Option(None, help="ISO datetime"),
    until: Optional[str] = typer.Option(None),
    # pagination
    limit: int = typer.Option(20, help="page size"),
    offset: int = typer.Option(0),
    sort: str = typer.Option("started_at", help="started_at | duration_ms | total_tokens_paid | …"),
    order: str = typer.Option("desc", help="asc | desc"),
    db: Optional[str] = typer.Option(None, "--db", help="trace DB path; defaults to ~/.diffgraph/traces.db"),
):
    """List runs across all five §5e.11 search dimensions."""
    f = RunFilter(
        kind=kind, agent_name=agent, model=model, status=status,
        generation=generation, mutation=mutation,
        project=project, file=file, jira=jira,
        scenario_id=scenario, scenario_tag=scenario_tag, pr_url=pr_url,
        duration_gt_ms=duration_gt_ms, tokens_gt=tokens_gt,
        since=since, until=until,
        limit=limit, offset=offset, sort=sort, order=order,
    )
    store = _store(db)
    rows = store.list_runs(f)
    total = store.count_runs(f)

    meta = {"total": total, "limit": limit, "offset": offset,
            "has_more": (offset + len(rows)) < total}

    if _Out.json_mode:
        _emit(rows, meta=meta)
        return

    if not rows:
        _emit([], suggestion="try --kind=agent or drop some filters")
        return

    table = Table(title=f"runs · {total} total · showing {offset}–{offset + len(rows) - 1}",
                  show_lines=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("kind")
    table.add_column("agent")
    table.add_column("scenario")
    table.add_column("model")
    table.add_column("mutation")
    table.add_column("dur ms", justify="right")
    table.add_column("status")
    table.add_column("started")
    for r in rows:
        table.add_row(
            (r.get("id") or "")[:12],
            r.get("kind") or "",
            r.get("agent_name") or "",
            r.get("scenario_id") or "",
            (r.get("model") or "")[:18],
            (r.get("mutation") or "")[:8],
            str(r.get("duration_ms") or ""),
            r.get("status") or "",
            (r.get("started_at") or "")[:19],
        )
    _Out.console.print(table)
    if meta["has_more"]:
        _Out.console.print(f"[dim]… {total - offset - len(rows)} more (use --offset={offset + len(rows)})[/dim]")


# ── runs get ──────────────────────────────────────────────────────────────

@runs_app.command("get")
def runs_get(
    run_id: str = typer.Argument(...),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Show one run, including FS trace path and all dimension columns."""
    r = _store(db).get_run(run_id)
    if not r:
        _emit_error("not_found", f"run {run_id} not found")
    if _Out.json_mode:
        _emit(r)
        return
    # Human view — pretty key/value table
    table = Table(title=f"run {r['id']}", show_header=False, expand=True)
    table.add_column("k", style="cyan", no_wrap=True)
    table.add_column("v", style="white")
    for k, v in r.items():
        if v is None or v == "":
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        s = str(v)
        if len(s) > 200:
            s = s[:197] + "…"
        table.add_row(k, s)
    _Out.console.print(table)
    if r.get("fs_trace_path"):
        _Out.console.print(f"\n[dim]↗ fs trace: [cyan]{r['fs_trace_path']}[/cyan][/dim]")


# ── tool-calls list ───────────────────────────────────────────────────────

@tools_app.command("list")
def tools_list(
    tool: Optional[str] = typer.Option(None, help="exact tool name"),
    agent: Optional[str] = typer.Option(None),
    model: Optional[str] = typer.Option(None),
    args_contains: Optional[str] = typer.Option(None, help="substring match in args JSON"),
    since: Optional[str] = typer.Option(None),
    until: Optional[str] = typer.Option(None),
    limit: int = typer.Option(20),
    offset: int = typer.Option(0),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Cross-run tool-call examples — find request/response pairs.

    Most useful for "show me how reflect args look on qwen3-6" or
    "find all calls to read_file with arg containing PricingService".
    """
    f = ToolCallFilter(
        tool=tool, agent_name=agent, model=model,
        args_contains=args_contains, since=since, until=until,
        limit=limit, offset=offset,
    )
    hits = _store(db).search_tool_calls(f)
    meta = {"limit": limit, "offset": offset, "returned": len(hits)}

    if _Out.json_mode:
        _emit(hits, meta=meta)
        return

    if not hits:
        _emit([], suggestion="check --tool name (use `quality-cli aggregates by-tool` once we add it)")
        return

    table = Table(title=f"tool-calls · {len(hits)} hits")
    table.add_column("run", style="cyan", no_wrap=True)
    table.add_column("step", justify="right")
    table.add_column("agent")
    table.add_column("tool")
    table.add_column("model")
    table.add_column("scenario")
    table.add_column("args (preview)")
    for h in hits:
        args = h.get("args")
        if isinstance(args, dict):
            args_preview = ", ".join(f"{k}={str(v)[:30]}" for k, v in list(args.items())[:2])
        else:
            args_preview = str(args)[:60]
        table.add_row(
            (h.get("run_id") or "")[:12],
            str(h.get("step") or ""),
            h.get("agent_name") or "",
            h.get("tool") or "",
            (h.get("model") or "")[:18],
            h.get("scenario_id") or "",
            args_preview,
        )
    _Out.console.print(table)


# ── aggregates ────────────────────────────────────────────────────────────

@agg_app.command("by-provider")
def agg_provider(
    kind: Optional[str] = typer.Option(None),
    since: Optional[str] = typer.Option(None),
    until: Optional[str] = typer.Option(None),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Per-provider stats: runs, avg duration, completion rate."""
    f = RunFilter(kind=kind, since=since, until=until, limit=10**9)
    rows = _store(db).aggregate_by_provider(f)
    if _Out.json_mode:
        _emit(rows)
        return
    if not rows:
        _emit([], suggestion="no runs match the filter")
        return
    table = Table(title="by provider")
    table.add_column("model", style="cyan")
    table.add_column("runs", justify="right")
    table.add_column("avg dur ms", justify="right")
    table.add_column("avg tokens", justify="right")
    table.add_column("completed", justify="right")
    table.add_column("errored", justify="right")
    for r in rows:
        table.add_row(
            (r.get("model") or "")[:35],
            str(r["runs"]),
            f"{r.get('avg_duration_ms') or 0:.0f}",
            f"{r.get('avg_tokens') or 0:.0f}",
            str(r["completed"]),
            str(r["errored"]),
        )
    _Out.console.print(table)


@agg_app.command("by-scenario")
def agg_scenario(
    generation: Optional[str] = typer.Option(None),
    since: Optional[str] = typer.Option(None),
    until: Optional[str] = typer.Option(None),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Per-scenario stats: runs, avg duration, completed count."""
    f = RunFilter(generation=generation, since=since, until=until, limit=10**9)
    rows = _store(db).aggregate_by_scenario(f)
    if _Out.json_mode:
        _emit(rows)
        return
    if not rows:
        _emit([], suggestion="no runs have a scenario_id yet")
        return
    table = Table(title="by scenario")
    table.add_column("scenario", style="cyan")
    table.add_column("runs", justify="right")
    table.add_column("avg dur ms", justify="right")
    table.add_column("completed", justify="right")
    for r in rows:
        table.add_row(
            r.get("scenario_id") or "",
            str(r["runs"]),
            f"{r.get('avg_duration_ms') or 0:.0f}",
            str(r["completed"]),
        )
    _Out.console.print(table)


# ── traces ────────────────────────────────────────────────────────────────
# All `traces …` commands talk to the FastAPI server (CLI + UI go
# through one HTTP API) — never to SQLite directly. Set the server
# URL via QUALITY_API_URL or --server (default localhost:8765).

import os as _os_traces
import httpx as _httpx


def _api_base() -> str:
    return _os_traces.environ.get("QUALITY_API_URL",
                                   "http://localhost:8765").rstrip("/")


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET <api>/path?k=v… — returns parsed JSON. Raises on HTTP error."""
    url = _api_base() + path
    try:
        r = _httpx.get(url, params=params or {}, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except _httpx.RequestError as exc:
        _emit_error(
            "api_unreachable",
            f"can't reach {url} ({exc}). Set QUALITY_API_URL or start "
            f"the server (./dev_server.sh start).",
        )
    except _httpx.HTTPStatusError as exc:
        _emit_error("api_error",
                    f"{exc.response.status_code} from {url}: "
                    f"{exc.response.text[:200]}")


@traces_app.command("ls")
def traces_ls(
    kind: Optional[str] = typer.Option(None, help="agent | judge"),
    agent: Optional[str] = typer.Option(None,
                                          help="dispatcher | reviewer | investigator | judge"),
    status: Optional[str] = typer.Option(None, help="completed | error | running"),
    scenario: Optional[str] = typer.Option(None),
    mutation: Optional[str] = typer.Option(None),
    lineage: Optional[str] = typer.Option(None,
                                            help="git branch (= lineage in our model)"),
    plan: Optional[int] = typer.Option(None),
    task: Optional[int] = typer.Option(None),
    session: Optional[str] = typer.Option(None,
                                            help="show all sub-agents of this run_id"),
    scenario_run: Optional[str] = typer.Option(None,
                                                 help="agent + judge pair (one attempt)"),
    since: Optional[str] = typer.Option(None, help="ISO datetime"),
    limit: int = typer.Option(20),
    offset: int = typer.Option(0),
    order: str = typer.Option("desc", help="asc | desc on started_at"),
):
    """List sub-agent rows from /api/search/sub_runs.

    The default unit is one (run_id × agent_id) tuple — i.e. one
    sub-agent of one CLI session. Use --session to drill into all
    sub-agents of a single session, or --scenario_run to see an
    (agent + judge) pair.
    """
    params = {k: v for k, v in {
        "kind": kind, "agent": agent, "status": status,
        "scenario": scenario, "mutation": mutation,
        "session": session, "scenario_run": scenario_run,
        "plan": plan, "task": task,
        "since": since,
        "limit": limit, "offset": offset, "order": order,
    }.items() if v is not None}
    j = _api_get("/api/search/sub_runs", params)
    if _Out.json_mode:
        _emit(j.get("data", []), meta=j.get("meta"))
        return
    rows = j.get("data", [])
    if not rows:
        _emit([], suggestion="no traces match — drop filters or try `ls --status=error`")
        return
    table = Table(title=f"sub-agents · {(j.get('meta') or {}).get('total', len(rows))}")
    for col in ("session", "agent_id", "kind", "agent", "scenario",
                "mutation", "plan", "task", "events", "started"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            (r.get("session_id") or "")[:12],
            (r.get("agent_id") or "")[:8],
            r.get("kind") or "",
            r.get("agent_name") or "",
            r.get("scenario_id") or "",
            (r.get("mutation") or "")[:7],
            f"#{r['plan_id']}" if r.get("plan_id") else "",
            f"#{r['task_id']}" if r.get("task_id") else "",
            str(r.get("events_count") or ""),
            (r.get("started_at") or "")[:19],
        )
    _Out.console.print(table)


@traces_app.command("session")
def traces_session(run_id: str = typer.Argument(...)):
    """Render one session as a sub-agent tree. Drill into specific
    spans via `traces events` or `traces event`."""
    j = _api_get("/api/search/sub_runs", {"session": run_id, "limit": 200})
    rows = j.get("data", [])
    if _Out.json_mode:
        _emit(rows, meta=j.get("meta"))
        return
    if not rows:
        _emit([], suggestion=f"no sub-agents for run {run_id}")
        return
    _Out.console.print(f"[bold]session[/bold] [cyan]{run_id}[/cyan]")
    # Group by agent_name to produce: dispatcher (root) → reviewer → investigator-N
    # For Phase 0 we don't have parent info; show a flat list ordered by start.
    for r in rows:
        prefix = "  " if r.get("agent_name") not in ("dispatcher", "reviewer") else ""
        ev = r.get("events_count") or 0
        dur_ms = r.get("duration_ms")
        dur = f"{dur_ms}ms" if dur_ms else "—"
        _Out.console.print(
            f"{prefix}[green]{r.get('agent_name'):>14}[/green] "
            f"[dim]{(r.get('agent_id') or '')[:8]}[/dim]  "
            f"events=[cyan]{ev}[/cyan] dur=[yellow]{dur}[/yellow]  "
            f"started=[dim]{(r.get('started_at') or '')[:19]}[/dim]"
        )


@traces_app.command("events")
def traces_events(
    run_id: str = typer.Argument(...),
    agent_id: Optional[str] = typer.Option(None,
                                             help="filter to one sub-agent"),
    event_type: Optional[str] = typer.Option(None,
                                                help="agent_llm_request | agent_tool_request | …"),
    limit: int = typer.Option(50),
):
    """List events of one session. Each event is one step / LLM call /
    tool result / etc. — the raw audit trail an agent uses to debug."""
    events = _api_get(f"/api/runs/{run_id}/events")
    if not isinstance(events, list):
        events = events.get("data", []) if isinstance(events, dict) else []
    filtered = []
    for e in events:
        if agent_id and e.get("agent_id") != agent_id:
            continue
        if event_type and e.get("event_type") != event_type:
            continue
        filtered.append(e)
        if len(filtered) >= limit:
            break
    if _Out.json_mode:
        _emit(filtered, meta={"total_in_run": len(events), "returned": len(filtered)})
        return
    if not filtered:
        _emit([], suggestion="no events match — drop filter")
        return
    table = Table(title=f"events · run {run_id} · {len(filtered)}/{len(events)}")
    for col in ("ev_id", "step", "agent", "agent_id", "event_type", "ts"):
        table.add_column(col)
    for e in filtered:
        table.add_row(
            str(e.get("id") or ""),
            str(e.get("step") or ""),
            (e.get("agent_name") or ""),
            (e.get("agent_id") or "")[:8],
            e.get("event_type") or "",
            (e.get("timestamp") or "")[:19],
        )
    _Out.console.print(table)


@traces_app.command("event")
def traces_event(
    run_id: str = typer.Argument(...),
    event_id: int = typer.Argument(...),
):
    """Fetch one event with its full `data` payload (LLM messages,
    tool args, tool results, etc.). The granular building block an
    AI debugger uses to root-cause a failure."""
    events = _api_get(f"/api/runs/{run_id}/events")
    if not isinstance(events, list):
        events = events.get("data", []) if isinstance(events, dict) else []
    matching = next((e for e in events if e.get("id") == event_id), None)
    if not matching:
        _emit_error("not_found", f"event #{event_id} not in run {run_id}")
    if _Out.json_mode:
        _emit(matching)
        return
    _Out.console.print(f"[bold]event #{matching['id']}[/bold] "
                        f"[dim]run={run_id}[/dim]")
    _Out.console.print(f"  type: [cyan]{matching['event_type']}[/cyan]  "
                        f"step={matching.get('step')}  "
                        f"agent={matching.get('agent_name')}/"
                        f"{(matching.get('agent_id') or '')[:8]}")
    _Out.console.print(f"  ts:   {matching['timestamp']}")
    _Out.console.print(f"\n[dim]data:[/dim]")
    import json as _j
    _Out.console.print(_j.dumps(matching.get("data") or {}, indent=2,
                                 ensure_ascii=False))


@traces_app.command("otel")
def traces_otel(run_id: str = typer.Argument(...)):
    """Dump one session as OTLP-JSON. Pipe to a Jaeger/Tempo collector
    or any OTel-aware tool: `quality-cli traces otel X | jq …`."""
    j = _api_get(f"/api/otel/traces/{run_id}")
    import json as _j
    _Out.console.print(_j.dumps(j, indent=2, ensure_ascii=False))


@traces_app.command("problems")
def traces_problems(
    since: Optional[str] = typer.Option(None,
                                          help="ISO datetime; defaults to last 24h"),
    limit: int = typer.Option(20),
    score_threshold: float = typer.Option(0.5,
                                            help="judge overall_score below this counts as bad"),
):
    """Surface candidates for AI-debug attention.

    Probes:
      1. Runs with status='error' (uncaught exceptions)
      2. Tasks stuck in 'leased' / 'running' beyond their lease
      3. Dead workers from list_workers' self-classification
      4. Mutations whose mean judge score is below threshold

    JSON output groups them under {errored_runs, stuck_tasks,
    dead_workers, low_score_mutations} so an AI agent can iterate.
    """
    out = {"errored_runs": [], "stuck_tasks": [],
           "dead_workers": [], "low_score_mutations": []}
    # 1. Errored runs
    j = _api_get("/api/search/runs",
                 {"status": "error", "limit": limit,
                  **({"since": since} if since else {})})
    out["errored_runs"] = j.get("data", [])
    # 2. Stuck leased tasks
    tasks_j = _api_get("/api/qa/tasks",
                        {"state": "leased", "limit": limit})
    if isinstance(tasks_j, dict):
        tasks = tasks_j.get("data", [])
    else:
        tasks = tasks_j or []
    out["stuck_tasks"] = tasks
    # 3. Dead workers
    workers_j = _api_get("/api/qa/workers")
    workers = workers_j.get("data", []) if isinstance(workers_j, dict) else []
    out["dead_workers"] = [w for w in workers if w.get("health") == "dead"]
    # 4. Low-score mutations
    muts_j = _api_get("/api/search/aggregates/by_mutation", {"limit": 100})
    muts = muts_j.get("data", []) if isinstance(muts_j, dict) else []
    low = []
    for m in muts:
        try:
            s_j = _api_get(f"/api/search/scoring/{m['mutation']}")
            score = ((s_j.get("data") or {}).get("axes") or {}).get("hard_skill")
            if score is not None and score < score_threshold:
                low.append({"mutation": m["mutation"],
                            "hard_skill": score,
                            "runs": m.get("runs"),
                            "lineages": m.get("lineages")})
        except SystemExit:  # _emit_error inside _api_get
            pass
    out["low_score_mutations"] = low[:limit]

    if _Out.json_mode:
        _emit(out)
        return
    _Out.console.print("[bold]🔍 problems probe[/bold]")
    _Out.console.print(f"  errored runs:        [red]{len(out['errored_runs'])}[/red]")
    _Out.console.print(f"  stuck leased tasks:  [yellow]{len(out['stuck_tasks'])}[/yellow]")
    _Out.console.print(f"  dead workers:        [red]{len(out['dead_workers'])}[/red]")
    _Out.console.print(f"  low-score mutations: "
                        f"[yellow]{len(out['low_score_mutations'])}[/yellow] "
                        f"(< {score_threshold:.2f})")
    if out["errored_runs"]:
        _Out.console.print("\n[dim]errored runs (first 5):[/dim]")
        for r in out["errored_runs"][:5]:
            _Out.console.print(
                f"  [red]✗[/red] {r['id'][:12]} {r.get('agent_name')} "
                f"scen={r.get('scenario_id')} mut={r.get('mutation')}"
            )
    if out["low_score_mutations"]:
        _Out.console.print("\n[dim]low-score mutations:[/dim]")
        for m in out["low_score_mutations"][:5]:
            _Out.console.print(
                f"  [yellow]·[/yellow] mutation {m['mutation']} "
                f"hard_skill={m['hard_skill']:.2f} runs={m['runs']}"
            )


# ── plans ─────────────────────────────────────────────────────────────────

from quality_api.queue import (  # noqa: E402
    TaskQueue, PlanStore, PlanSpec, TaskSpec, plan_to_dict, task_to_dict,
)


def _queue() -> TaskQueue:
    return TaskQueue()


@plans_app.command("create")
def plans_create(
    providers: str = typer.Option(..., help="comma-separated, e.g. deepseek,qwen3-6"),
    scenarios: str = typer.Option(..., help="comma-separated scenario ids"),
    lineages: Optional[str] = typer.Option(None, help="comma-separated lineages (typically git branches like `master`); omit for lineage-less scenarios"),
    attempts_min: int = typer.Option(1, help="attempts per (lineage × provider × scenario) cell"),
    priority: int = typer.Option(100),
    name: Optional[str] = typer.Option(None),
    notes: Optional[str] = typer.Option(None),
    created_by: str = typer.Option("cli"),
):
    """Create a plan — a cross-product of (lineages × providers × scenarios × attempts_min).

    Each cell becomes one task in qa_tasks tagged with the new plan_id.
    """
    spec = PlanSpec(
        name=name or "",
        created_by=created_by,
        lineages=_split_csv(lineages),
        providers=_split_csv(providers),
        scenarios=_split_csv(scenarios),
        attempts_min=attempts_min,
        priority=priority,
        notes=notes or "",
    )
    plans = PlanStore(_queue())
    try:
        plan_id, task_ids = plans.create(spec)
    except ValueError as e:
        _emit_error("invalid_plan", str(e))
    p = plans.get(plan_id)
    progress = plans.progress(plan_id)
    if _Out.json_mode:
        _emit(plan_to_dict(p, progress=progress),
              meta={"task_ids": task_ids})
        return
    _Out.console.print(f"[green]created plan[/green] [cyan]#{plan_id}[/cyan] · {len(task_ids)} task(s) enqueued")
    _Out.console.print(f"  providers: {', '.join(p.providers)}")
    _Out.console.print(f"  scenarios: {', '.join(p.scenarios)}")
    if p.lineages:
        _Out.console.print(f"  lineages:  {', '.join(p.lineages)}")
    _Out.console.print(f"  attempts_min: {p.attempts_min}")


@plans_app.command("list")
def plans_list(
    state: Optional[str] = typer.Option(None),
    limit: int = typer.Option(20),
):
    plans = PlanStore(_queue())
    rows = plans.list(state=state, limit=limit)
    data = [plan_to_dict(p, progress=plans.progress(p.id)) for p in rows]
    if _Out.json_mode:
        _emit(data)
        return
    if not data:
        _emit([], suggestion="no plans yet — try `quality-cli plans create ...`")
        return
    table = Table(title=f"plans · {len(data)}")
    table.add_column("id", style="cyan", justify="right")
    table.add_column("name")
    table.add_column("state")
    table.add_column("providers")
    table.add_column("scenarios")
    table.add_column("queued/leased/done", justify="right")
    table.add_column("created")
    for d in data:
        prog = d["progress"]
        bar = f"{prog.get('queued', 0)}/{prog.get('leased', 0) + prog.get('running', 0)}/{prog.get('finished', 0)}"
        table.add_row(
            str(d["id"]),
            (d["name"] or "")[:30],
            d["state"],
            ",".join(d["providers"])[:20],
            ",".join(d["scenarios"])[:30],
            bar,
            d["created_at"][:19],
        )
    _Out.console.print(table)


@plans_app.command("get")
def plans_get(plan_id: int = typer.Argument(...)):
    plans = PlanStore(_queue())
    p = plans.get(plan_id)
    if not p:
        _emit_error("not_found", f"plan {plan_id} not found")
    progress = plans.progress(plan_id)
    eta = plans.eta(plan_id)
    if _Out.json_mode:
        _emit(plan_to_dict(p, progress=progress, eta=eta))
        return
    _Out.console.print(f"[bold]plan #{p.id}[/bold] · state=[cyan]{p.state}[/cyan]")
    _Out.console.print(f"  name: {p.name or '(unnamed)'}")
    _Out.console.print(f"  created: {p.created_at} by {p.created_by or '?'}")
    _Out.console.print(f"  providers: {', '.join(p.providers)}")
    _Out.console.print(f"  scenarios: {', '.join(p.scenarios)}")
    if p.lineages:
        _Out.console.print(f"  lineages:  {', '.join(p.lineages)}")
    _Out.console.print(f"  attempts_min: {p.attempts_min}")
    _Out.console.print(f"\n[dim]progress:[/dim] {progress}")
    if eta.get("remaining_tasks"):
        _Out.console.print(
            f"[dim]eta:[/dim] [yellow]~{eta['eta_seconds'] // 60}m[/yellow] "
            f"({eta['remaining_tasks']} task(s) left, "
            f"based on {eta['based_on']['history_runs']} historical runs)"
        )
        for pp in eta["per_provider"]:
            _Out.console.print(
                f"  · {pp['provider']}: {pp['remaining_tasks']} tasks · "
                f"{pp['workers']} worker(s) → ~{pp['eta_seconds']//60}m"
            )


@plans_app.command("cancel")
def plans_cancel(plan_id: int = typer.Argument(...)):
    n = PlanStore(_queue()).cancel(plan_id)
    if _Out.json_mode:
        _emit({"cancelled_tasks": n})
        return
    _Out.console.print(f"[yellow]cancelled[/yellow] plan #{plan_id} · {n} task(s) marked cancelled")


@plans_app.command("watch")
def plans_watch(
    plan_id: int = typer.Argument(...),
    interval: float = typer.Option(2.0, help="poll interval in seconds"),
):
    """Live progress bar for a plan. Polls the queue every <interval>s
    and renders a Rich bar with completed/total counts + ETA. Exits
    when the plan reaches a terminal state."""
    import time
    from rich.progress import (Progress, BarColumn, TextColumn,
                                TaskProgressColumn, TimeRemainingColumn)
    plans = PlanStore(_queue())
    p = plans.get(plan_id)
    if not p:
        _emit_error("not_found", f"plan {plan_id} not found")

    if _Out.json_mode:
        # JSON-mode: emit one snapshot per poll until terminal.
        while True:
            p = plans.get(plan_id)
            prog = plans.progress(plan_id)
            eta = plans.eta(plan_id)
            _emit(plan_to_dict(p, progress=prog, eta=eta))
            if p.state in ("done", "cancelled"):
                break
            time.sleep(interval)
        return

    progress_widget = Progress(
        TextColumn("[bold]plan #{task.fields[plan_id]}[/bold]"),
        BarColumn(bar_width=40),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[breakdown]}[/dim]"),
        TextColumn("eta [yellow]{task.fields[eta_text]}[/yellow]"),
        TimeRemainingColumn(),
        console=_Out.console,
    )
    with progress_widget as bar:
        prog = plans.progress(plan_id)
        eta = plans.eta(plan_id)
        total = max(1, prog.get("total", 1))
        done = (prog.get("finished", 0) + prog.get("error", 0)
                + prog.get("cancelled", 0))
        task = bar.add_task("plan", total=total, completed=done,
                            plan_id=plan_id, breakdown="", eta_text="…")
        while True:
            p = plans.get(plan_id)
            prog = plans.progress(plan_id)
            eta = plans.eta(plan_id)
            done = (prog.get("finished", 0) + prog.get("error", 0)
                    + prog.get("cancelled", 0))
            breakdown = (f"finished={prog.get('finished',0)} "
                         f"running={prog.get('running',0)+prog.get('leased',0)} "
                         f"queued={prog.get('queued',0)} "
                         f"errored={prog.get('error',0)} "
                         f"cancelled={prog.get('cancelled',0)}")
            eta_s = eta.get("eta_seconds", 0)
            eta_text = ("done" if not eta.get("remaining_tasks") else
                        (f"~{eta_s}s" if eta_s < 60 else
                         f"~{eta_s//60}m" if eta_s < 3600 else
                         f"~{eta_s/3600:.1f}h"))
            bar.update(task, completed=done, total=max(total, prog.get("total", total)),
                       breakdown=breakdown, eta_text=eta_text)
            if p.state in ("done", "cancelled"):
                break
            time.sleep(interval)
    _Out.console.print(f"\n[bold]final:[/bold] state=[cyan]{p.state}[/cyan]  {breakdown}")


# ── tasks ─────────────────────────────────────────────────────────────────

@tasks_app.command("list")
def tasks_list(
    state: Optional[str] = typer.Option(None),
    provider: Optional[str] = typer.Option(None),
    plan_id: Optional[int] = typer.Option(None),
    limit: int = typer.Option(20),
):
    rows = _queue().list(state=state, provider=provider, plan_id=plan_id, limit=limit)
    data = [task_to_dict(r) for r in rows]
    if _Out.json_mode:
        _emit(data)
        return
    if not data:
        _emit([], suggestion="no tasks yet — try `quality-cli plans create ...`")
        return
    table = Table(title=f"tasks · {len(data)}")
    table.add_column("id", style="cyan", justify="right")
    table.add_column("state")
    table.add_column("provider")
    table.add_column("scenario")
    table.add_column("attempt", justify="right")
    table.add_column("plan", justify="right")
    table.add_column("worker")
    table.add_column("trace_run_id")
    for d in data:
        table.add_row(
            str(d["id"]),
            d["state"],
            d["provider"],
            d["scenario_id"],
            str(d["attempt_n"]),
            str(d["plan_id"] or ""),
            (d.get("lease_owner") or "")[:14],
            (d.get("trace_run_id") or "")[:14],
        )
    _Out.console.print(table)


@tasks_app.command("reap")
def tasks_reap(grace_seconds: int = typer.Option(30)):
    """Recover stale-leased tasks back to queued."""
    n = _queue().reap_stale_leases(grace_seconds=grace_seconds)
    if _Out.json_mode:
        _emit({"reaped": n})
        return
    _Out.console.print(f"[green]reaped[/green] {n} stale lease(s)")


# ── worker ────────────────────────────────────────────────────────────────

@app.command("worker")
def worker_loop(
    provider: str = typer.Option(..., help="LLM provider this worker serves"),
    capacity: int = typer.Option(1, help="(reserved — single in-flight task for now)"),
    bench_cmd: str = typer.Option(
        "cd /home/andrey/repos/code-review-benchmarks "
        "&& source .env "
        "&& unset ALL_PROXY all_proxy "
        "&& .venv/bin/python benchmark/cli.py run -s {scenario} -p {provider}",
        help="Shell command template; {scenario} and {provider} are substituted from the task. "
             "Default invokes code-review-benchmarks; override to plug in a different runner.",
    ),
    poll_seconds: int = typer.Option(3, help="poll interval when queue is empty"),
    lease_seconds: int = typer.Option(120, help="lease duration; heartbeat resets it"),
    heartbeat_seconds: int = typer.Option(30, help="heartbeat interval"),
    max_tasks: int = typer.Option(0, help="stop after N tasks (0 = forever)"),
    max_idle_seconds: int = typer.Option(0,
        help="exit after N consecutive seconds of empty queue (0 = forever). "
             "Used by the server supervisor to spin workers down when the "
             "queue dries up. Set to 120 for typical pool-managed workers."),
    task_timeout_seconds: int = typer.Option(900,
        help="hard cap on a single bench subprocess (seconds). "
             "Beyond this we kill the LLM and mark the task as error so "
             "a stuck provider doesn't hold the slot indefinitely."),
):
    """Long-running worker: lease task → run bench → finish.

    Talks directly to the local TaskQueue (offline mode). HTTP variant
    will land alongside the `--server` flag; same loop, different
    transport for lease/heartbeat/finish.
    """
    import os
    import signal
    import subprocess
    import threading
    import time

    q = _queue()
    wid = q.register_worker(provider=provider, capacity=capacity, pid=os.getpid())
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except Exception:
            pass

    if not _Out.json_mode:
        _Out.console.print(f"[green]worker[/green] [cyan]{wid}[/cyan] · provider={provider} · pid={os.getpid()}")
        _Out.console.print(f"[dim]bench_cmd template: {bench_cmd}[/dim]")
        _Out.console.print(f"[dim]ctrl-c to stop · max_tasks={max_tasks or '∞'}[/dim]")

    completed = 0
    idle_since: float | None = None  # set when first idle poll seen
    import time as _time
    while not stop.is_set():
        if max_tasks and completed >= max_tasks:
            break

        # Worker heartbeat — keep alive in qa_workers regardless of leasing.
        q.worker_heartbeat(wid)

        t = q.lease(provider=provider, worker_id=wid, lease_seconds=lease_seconds)
        if t is None:
            if idle_since is None:
                idle_since = _time.monotonic()
            elif max_idle_seconds and (_time.monotonic() - idle_since) >= max_idle_seconds:
                if not _Out.json_mode:
                    _Out.console.print(f"[dim]· {datetime_now()} idle for {max_idle_seconds}s — exiting[/dim]")
                break
            if not _Out.json_mode:
                _Out.console.print(f"[dim]· idle · {datetime_now()}[/dim]")
            stop.wait(poll_seconds)
            continue
        idle_since = None  # got work, reset idle counter

        if not _Out.json_mode:
            _Out.console.print(f"[green]→ leased[/green] task #{t.id} scenario={t.scenario_id} attempt={t.attempt_n}")

        # Heartbeat thread — extends task lease AND refreshes the
        # worker row while bench runs. Without the worker_heartbeat
        # call here, list_workers' stale_after_seconds (90s) would
        # falsely mark the worker as 'dead' during long tests.
        hb_stop = threading.Event()
        def _hb():
            while not hb_stop.is_set():
                q.worker_heartbeat(wid)
                if not q.heartbeat(t.id, worker_id=wid, lease_seconds=lease_seconds):
                    # Task got reaped or finished — signal worker to abort.
                    hb_stop.set()
                    return
                hb_stop.wait(heartbeat_seconds)
        hb_thread = threading.Thread(target=_hb, daemon=True)
        hb_thread.start()

        # Build + run bench command.
        cmd = bench_cmd.format(scenario=t.scenario_id, provider=t.provider,
                               lineage=t.lineage, mutation=t.mutation_hash,
                               attempt=t.attempt_n)
        result_state = "finished"
        error_class = None
        result_payload: dict = {}
        # Pass the git commit SHA (when this task came from auto-plan)
        # to diff-graph so its runs.mutation is keyed by commit instead
        # of by prompt-content-hash. Without this, multiple plans on
        # different commits with identical prompt content collapse to
        # one mutation row in the analytics.
        env = dict(os.environ)
        # LLM / Bitbucket / scenario PR endpoints all live on direct
        # routes; the user's local proxy chain (HTTP_PROXY / HTTPS_PROXY
        # / ALL_PROXY → http://127.0.0.1:* + a self-signed CA on the
        # MITM box) breaks api.deepseek.com calls with
        # CERTIFICATE_VERIFY_FAILED. Strip proxy vars from the bench
        # env so EVERYTHING the subprocess does (agent cli.py, judge
        # cli.py, bench's own LLM/Bitbucket calls) goes straight out.
        for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy"):
            env.pop(_v, None)
        if t.mutation_hash:
            env["DIFFGRAPH_MUTATION_OVERRIDE"] = t.mutation_hash
        # Pre-generate the diff-graph run_id and stamp it onto qa_tasks
        # NOW (before the bench subprocess starts). Diff-graph reads
        # the same id from env and uses it as its run_id, so /qa/runs
        # can show "task #N → run X" the moment the agent starts
        # emitting events — no waiting for finish, no mutation+window
        # guessing, no cross-task bleed when scenarios overlap in time.
        import uuid as _uuid
        pre_run_id = str(_uuid.uuid4())[:12]
        env["DIFFGRAPH_TRACE_RUN_ID"] = pre_run_id
        try:
            q.set_task_trace_run_id(t.id, pre_run_id)
        except Exception:
            pass
        # OTel span around this task's bench subprocess. Stamp the
        # domain dims (plan/task/scenario/mutation/lineage) so the
        # worker.task span AND any spans inside (the bench → cli.py
        # → agent.* chain via TRACEPARENT) carry the same identity
        # — single denormalised otel_spans table answers all queries.
        try:
            from orchestra.otel import (setup_tracing, set_domain_attrs,
                                          current_traceparent)
            _otel_tracer = setup_tracing("diffgraph-worker")
            set_domain_attrs(
                plan_id=t.plan_id,
                task_id=t.id,
                scenario_id=t.scenario_id or "",
                mutation=(t.mutation_hash or "")[:7] or None,
                lineage=t.lineage or "",
                provider=t.provider or "",
                run_id=pre_run_id,
            )
            _bench_span_cm = _otel_tracer.start_as_current_span(
                f"worker.task#{t.id}")
            _bench_span = _bench_span_cm.__enter__()
            env["TRACEPARENT"] = current_traceparent()
            # Also pass diffgraph.* dims as env so the bench → cli.py
            # subprocess can re-set domain attrs (TRACEPARENT carries
            # span context but not baggage; we keep it explicit).
            if t.plan_id is not None:
                env["DIFFGRAPH_PLAN_ID"] = str(t.plan_id)
            env["DIFFGRAPH_TASK_ID"] = str(t.id)
            if t.lineage:
                env["DIFFGRAPH_LINEAGE"] = t.lineage
        except Exception:
            _bench_span_cm = None
            _bench_span = None
        # Force bench to write judge artefacts to disk + trace DB. Without
        # this env var bench's session_dir stays None and the judge runs
        # the LLM call but never writes a `kind=judge` row, so /qa/plans
        # has no scoring data for these tasks.
        env.setdefault(
            "BENCHMARK_TRACE_DIR",
            str(Path.home() / ".diffgraph" / "bench-runs"),
        )
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True,
                env=env,
                timeout=task_timeout_seconds,
            )
            result_payload = {
                "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-500:],
            }
            if proc.returncode != 0:
                result_state = "error"
                error_class = "non_zero_exit"
        except subprocess.TimeoutExpired:
            result_state = "error"
            error_class = "timeout"
        except Exception as exc:
            result_state = "error"
            error_class = type(exc).__name__
            result_payload = {"exception": str(exc)}

        hb_stop.set()
        hb_thread.join(timeout=1)

        # Close the worker's OTel span; mark error status if the
        # subprocess failed/timed-out so the trace shows red.
        if _bench_span_cm is not None and _bench_span is not None:
            try:
                if result_state == "error":
                    from opentelemetry.trace import StatusCode, Status
                    _bench_span.set_status(
                        Status(StatusCode.ERROR, error_class or "")
                    )
                _bench_span.set_attribute("qa.result_state", result_state)
                _bench_span_cm.__exit__(None, None, None)
            except Exception:
                pass

        ok = q.finish(t.id, worker_id=wid, state=result_state,
                      result=result_payload, error_class=error_class)
        completed += 1
        if not _Out.json_mode:
            tag = "[green]✓ finished[/green]" if result_state == "finished" else f"[red]✗ {error_class}[/red]"
            _Out.console.print(f"  {tag} task #{t.id} · finish_ok={ok}")

    # Mark the worker row as cleanly stopped — distinguishes it in
    # the fleet UI from workers that died unexpectedly (state='dead'
    # from stale heartbeat).
    try:
        q.worker_set_state(wid, "stopped")
    except Exception:
        pass
    if not _Out.json_mode:
        _Out.console.print(f"[dim]worker {wid} stopped · completed={completed}[/dim]")


def datetime_now() -> str:
    """Compact timestamp for idle-loop logging — local time on purpose
    (this is console output for a human watching a worker, not data
    that goes into the DB)."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


# ── auto-plan ─────────────────────────────────────────────────────────────

from quality_api.discovery import AutoPlanStore, config_to_dict, git_list_branches  # noqa: E402


def _autostore() -> AutoPlanStore:
    return AutoPlanStore(_queue())


@auto_app.command("add")
def auto_add(
    repo: str = typer.Option(..., help="path to git repo to watch"),
    branch_pattern: str = typer.Option("master,feature/*",
        help="comma-separated globs"),
    bench_repo: str = typer.Option("",
        help="path to bench repo with benchmark/scenarios/*.yaml — required if using --tags"),
    providers: str = typer.Option(..., help="comma-separated"),
    scenarios: Optional[str] = typer.Option(None,
        help="explicit scenario ids (CSV); if set, --tags ignored"),
    tags: Optional[str] = typer.Option(None,
        help="scenario tags filter (CSV) — resolved at discover-time against --bench-repo"),
    min_gap_seconds: int = typer.Option(0,
        help="debounce per lineage: at most one plan per lineage per N seconds (0 = every commit)"),
    pacing: str = typer.Option("aggressive",
        help="'aggressive' (queue all immediately) or 'spread' (stagger over pacing-window-seconds)"),
    pacing_window_seconds: int = typer.Option(0,
        help="for pacing=spread: total seconds to stagger this plan's tasks over"),
    attempts_min: int = typer.Option(1),
    name: Optional[str] = typer.Option(None),
    enabled: bool = typer.Option(True),
):
    """Register a watched repo + branch pattern.

    Configs are open: each describes a filter (explicit scenarios OR
    tag selector) + cadence (min_gap_seconds debounce per lineage) +
    pacing (aggressive vs spread). Multiple configs run in parallel —
    typical setup is one config for sentinel-on-every-commit and
    another for full-slice-once-a-day.
    """
    try:
        cid = _autostore().add_config(
            name=name or "",
            repo_path=repo,
            branch_pattern=branch_pattern,
            bench_repo_path=bench_repo,
            providers=_split_csv(providers),
            scenarios=_split_csv(scenarios),
            scenario_tags=_split_csv(tags),
            min_gap_seconds=min_gap_seconds,
            pacing=pacing,
            pacing_window_seconds=pacing_window_seconds,
            attempts_min=attempts_min,
            enabled=enabled,
        )
    except ValueError as e:
        _emit_error("invalid", str(e))
    cfg = _autostore().get_config(cid)
    if _Out.json_mode:
        _emit(config_to_dict(cfg))
        return
    _Out.console.print(f"[green]created config[/green] [cyan]#{cid}[/cyan] · {cfg.name or '(unnamed)'}")
    _Out.console.print(f"  repo:     {cfg.repo_path}")
    _Out.console.print(f"  branches: {cfg.branch_pattern}")
    _Out.console.print(f"  providers: {', '.join(cfg.providers)}")
    if cfg.scenarios:
        _Out.console.print(f"  scenarios: {', '.join(cfg.scenarios)}")
    if cfg.scenario_tags:
        _Out.console.print(f"  tags: {', '.join(cfg.scenario_tags)} · bench={cfg.bench_repo_path or '(unset)'}")
    _Out.console.print(f"  cadence: min_gap={cfg.min_gap_seconds}s  pacing={cfg.pacing}"
                       + (f" window={cfg.pacing_window_seconds}s" if cfg.pacing == "spread" else ""))


@auto_app.command("edit")
def auto_edit(
    config_id: int = typer.Argument(...),
    name: Optional[str] = typer.Option(None),
    branch_pattern: Optional[str] = typer.Option(None),
    bench_repo: Optional[str] = typer.Option(None),
    providers: Optional[str] = typer.Option(None),
    scenarios: Optional[str] = typer.Option(None),
    tags: Optional[str] = typer.Option(None),
    min_gap_seconds: Optional[int] = typer.Option(None),
    pacing: Optional[str] = typer.Option(None),
    pacing_window_seconds: Optional[int] = typer.Option(None),
    attempts_min: Optional[int] = typer.Option(None),
):
    """Edit a config. Only fields you pass are touched."""
    fields = {}
    if name is not None: fields["name"] = name
    if branch_pattern is not None: fields["branch_pattern"] = branch_pattern
    if bench_repo is not None: fields["bench_repo_path"] = bench_repo
    if providers is not None: fields["providers"] = _split_csv(providers)
    if scenarios is not None: fields["scenarios"] = _split_csv(scenarios)
    if tags is not None: fields["scenario_tags"] = _split_csv(tags)
    if min_gap_seconds is not None: fields["min_gap_seconds"] = min_gap_seconds
    if pacing is not None: fields["pacing"] = pacing
    if pacing_window_seconds is not None: fields["pacing_window_seconds"] = pacing_window_seconds
    if attempts_min is not None: fields["attempts_min"] = attempts_min
    if not fields:
        _emit_error("no_op", "no fields to update")
    ok = _autostore().update_config(config_id, **fields)
    if not ok:
        _emit_error("not_found", f"config {config_id} not found")
    cfg = _autostore().get_config(config_id)
    if _Out.json_mode:
        _emit(config_to_dict(cfg))
        return
    _Out.console.print(f"[green]updated config[/green] #{config_id}")
    for k, v in fields.items():
        _Out.console.print(f"  {k}: {v}")


@auto_app.command("list")
def auto_list():
    cfgs = _autostore().list_configs()
    if _Out.json_mode:
        _emit([config_to_dict(c) for c in cfgs])
        return
    if not cfgs:
        _emit([], suggestion="add one via `quality-cli auto-plan add ...`")
        return
    table = Table(title=f"auto-plan configs · {len(cfgs)}")
    table.add_column("id", style="cyan", justify="right")
    table.add_column("name")
    table.add_column("repo")
    table.add_column("branches")
    table.add_column("unit/full")
    table.add_column("providers")
    table.add_column("enabled")
    table.add_column("last discover")
    for c in cfgs:
        table.add_row(
            str(c.id),
            c.name or "",
            c.repo_path[-30:],
            c.branch_pattern,
            f"{len(c.unit_scenarios)}/{len(c.full_scenarios)}",
            ",".join(c.providers),
            "✓" if c.enabled else "·",
            (c.last_discover_at or "")[:19],
        )
    _Out.console.print(table)


@auto_app.command("discover")
def auto_discover(
    config_id: Optional[int] = typer.Option(None, help="discover only this config"),
):
    """Manual discovery sweep — idempotent over qa_planned_commits."""
    created = _autostore().discover(config_id=config_id)
    if _Out.json_mode:
        _emit(created)
        return
    if not created:
        _Out.console.print("[dim]no new commits — nothing to plan[/dim]")
        return
    _Out.console.print(f"[green]created {len(created)} plan(s)[/green]")
    for p in created:
        _Out.console.print(
            f"  [cyan]#{p['plan_id']}[/cyan] {p['plan_kind']:5} {p['lineage']:35} "
            f"{p['sha'][:7]}  ({p['task_count']} task(s))"
        )


@auto_app.command("enable")
def auto_enable(config_id: int):
    ok = _autostore().set_enabled(config_id, True)
    if not ok:
        _emit_error("not_found", f"config {config_id} not found")
    if not _Out.json_mode:
        _Out.console.print(f"[green]enabled[/green] config #{config_id}")


@auto_app.command("disable")
def auto_disable(config_id: int):
    ok = _autostore().set_enabled(config_id, False)
    if not ok:
        _emit_error("not_found", f"config {config_id} not found")
    if not _Out.json_mode:
        _Out.console.print(f"[yellow]disabled[/yellow] config #{config_id}")


@auto_app.command("delete")
def auto_delete(config_id: int):
    ok = _autostore().delete_config(config_id)
    if not _Out.json_mode:
        msg = "[red]deleted[/red]" if ok else "[dim]nothing to delete[/dim]"
        _Out.console.print(f"{msg} config #{config_id}")


@auto_app.command("watch")
def auto_watch(
    interval: int = typer.Option(60, help="poll interval in seconds"),
    config_id: Optional[int] = typer.Option(None,
        help="only sweep this config (default: all enabled)"),
    once: bool = typer.Option(False, help="single sweep, then exit"),
):
    """Long-running poller: discover() every N seconds.

    State is fully in DB — restart-safe, never plans the same
    (lineage, sha, kind) twice. SIGINT/SIGTERM exits cleanly.
    """
    import signal, time, threading
    store = _autostore()
    stop = threading.Event()
    def _stop(signum, frame): stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, _stop)
        except Exception: pass

    if not _Out.json_mode:
        _Out.console.print(f"[green]watching[/green] · interval={interval}s · config={config_id or 'all'}")
        _Out.console.print(f"[dim]ctrl-c to stop{' · single sweep' if once else ''}[/dim]")

    sweeps = 0
    while not stop.is_set():
        try:
            created = store.discover(config_id=config_id)
        except Exception as exc:
            if not _Out.json_mode:
                _Out.console.print(f"[red]discover error:[/red] {type(exc).__name__}: {exc}")
            created = []
        sweeps += 1
        if _Out.json_mode:
            _emit({"sweep": sweeps, "created": created},
                  meta={"created_count": len(created)})
        else:
            ts = datetime_now()
            if created:
                _Out.console.print(f"[green]· {ts}[/green] sweep #{sweeps} → {len(created)} plan(s)")
                for p in created:
                    _Out.console.print(
                        f"    [cyan]#{p['plan_id']}[/cyan] {p['plan_kind']:5} "
                        f"{p['lineage']:30} {p['sha'][:7]} ({p['task_count']} tasks)"
                    )
            else:
                _Out.console.print(f"[dim]· {ts} sweep #{sweeps} — no new commits[/dim]")
        if once:
            break
        stop.wait(interval)

    if not _Out.json_mode:
        _Out.console.print(f"[dim]stopped after {sweeps} sweep(s)[/dim]")


@auto_app.command("branches")
def auto_branches(repo: str = typer.Argument(..., help="path to git repo")):
    """List branches + HEAD SHAs in the repo (debugging utility)."""
    pairs = git_list_branches(repo)
    if _Out.json_mode:
        _emit([{"branch": b, "sha": s} for b, s in pairs])
        return
    if not pairs:
        _Out.console.print(f"[dim]no branches found in {repo}[/dim]")
        return
    for b, sha in pairs:
        _Out.console.print(f"  [cyan]{sha[:7]}[/cyan]  {b}")


if __name__ == "__main__":
    app()
