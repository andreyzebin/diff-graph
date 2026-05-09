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
tools_app = typer.Typer(help="Cross-run tool-call search.", no_args_is_help=True)
agg_app = typer.Typer(help="Aggregate views.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")
app.add_typer(tools_app, name="tool-calls")
app.add_typer(agg_app, name="aggregates")


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
    gene: Optional[str] = typer.Option(None, help="comma-separated; ALL must be present"),
    gene_any: Optional[str] = typer.Option(None, help="comma-separated; ANY of these"),
    without_gene: Optional[str] = typer.Option(None, help="comma-separated; NONE of these"),
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
        genes=_split_csv(gene),
        genes_any=_split_csv(gene_any),
        without_gene=_split_csv(without_gene),
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


# ── genes ─────────────────────────────────────────────────────────────────

@app.command("genes")
def genes_list(
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Catalogue: every gene observed in any run, with run counts."""
    rows = _store(db).list_genes()

    if _Out.json_mode:
        _emit(rows)
        return

    if not rows:
        _emit([], suggestion="this DB has no runs with genes detected yet")
        return

    table = Table(title=f"genes · {len(rows)} observed")
    table.add_column("gene", style="cyan")
    table.add_column("runs", justify="right")
    for r in rows:
        table.add_row(r["gene"], str(r["runs_count"]))
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


@agg_app.command("by-gene")
def agg_gene(
    scenario_tag: Optional[str] = typer.Option(None, help="filter to runs with this scenario tag"),
    since: Optional[str] = typer.Option(None),
    until: Optional[str] = typer.Option(None),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Per-gene runs_with vs runs_without — substrate for evolution feedback."""
    f = RunFilter(scenario_tag=scenario_tag, since=since, until=until, limit=10**9)
    rows = _store(db).aggregate_by_gene(f)
    if _Out.json_mode:
        _emit(rows)
        return
    if not rows:
        _emit([], suggestion="no runs in scope have gene data")
        return
    table = Table(title="by gene")
    table.add_column("gene", style="cyan")
    table.add_column("with", justify="right")
    table.add_column("without", justify="right")
    for r in rows:
        table.add_row(r["gene"], str(r["runs_with"]), str(r["runs_without"]))
    _Out.console.print(table)


if __name__ == "__main__":
    app()
