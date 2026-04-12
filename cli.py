#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box as rich_box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app = typer.Typer(help="DiffGraph — multi-agent PR code review.", add_completion=False)
console = Console()

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"


# ── config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    import yaml

    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    cfg: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f) or {}

    local = CONFIG_FILE.with_name("config.local.yaml")
    if local.exists():
        with open(local) as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})

    return _expand_config(cfg)


def _expand_config(obj):
    if isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_config(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def _make_llm_client(llm_cfg: dict):
    from openai import OpenAI
    kwargs: dict = {"api_key": llm_cfg.get("api_key") or "no-key"}
    api_url = llm_cfg.get("api_url", "").strip()
    if api_url:
        kwargs["base_url"] = api_url
    return OpenAI(**kwargs)


def _read_diff(diff_path: Optional[str]) -> str:
    if diff_path is None or diff_path == "-":
        if sys.stdin.isatty():
            console.print("[red]No diff provided. Pass --diff <file> or pipe via stdin.[/red]")
            raise typer.Exit(1)
        return sys.stdin.read()
    p = Path(diff_path)
    if not p.exists():
        console.print(f"[red]Diff file not found: {diff_path}[/red]")
        raise typer.Exit(1)
    return p.read_text()


# ── commands ──────────────────────────────────────────────────────────────────

@app.command()
def run(
    repo:          Optional[str] = typer.Option(None,  "--repo",         "-r", help="Path to the repository"),
    diff:          Optional[str] = typer.Option(None,  "--diff",         "-d", help="Path to .diff file, or '-' for stdin"),
    pr_url:        Optional[str] = typer.Option(None,  "--pr-url",             help="Bitbucket Server PR URL — clones repo and fetches diff automatically"),
    model:         Optional[str] = typer.Option(None,  "--model",        "-m", help="LLM model override"),
    api_url:       Optional[str] = typer.Option(None,  "--api-url",            help="OpenAI-compatible API base URL override"),
    api_key:       Optional[str] = typer.Option(None,  "--api-key",            help="API key override"),
    output:        Optional[str] = typer.Option(None,  "--output",       "-o", help="Write findings as JSON to file"),
    post_comments: bool          = typer.Option(False, "--post-comments",      help="Post findings to the PR as inline comments (requires --pr-url)"),
    max_steps:     Optional[int] = typer.Option(None,  "--max-steps",          help="Max ReAct steps (default: from config)"),
    max_tokens:    Optional[int] = typer.Option(None,  "--max-tokens",         help="Max token budget (default: from config)"),
    prompts:       Optional[str] = typer.Option(None,  "--prompts",            help="Prompt resource URI (path, file://, bitbucket://)"),
):
    """
    Run a multi-agent PR review and print structured findings.

    \b
      git diff HEAD~1 | python cli.py run --repo .
      python cli.py run --repo ./my-service --diff changes.diff
      python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42
      python cli.py run --pr-url ... --post-comments
    """
    cfg = _load_config()
    llm_cfg    = cfg.get("llm", {})
    review_cfg = cfg.get("review", {})

    if api_url:  llm_cfg["api_url"] = api_url
    if api_key:  llm_cfg["api_key"] = api_key
    if model:    llm_cfg["model"]   = model

    effective_model     = llm_cfg.get("model", "gpt-4o-mini")
    effective_steps     = max_steps  if max_steps  is not None else review_cfg.get("max_steps",  40)
    effective_tokens    = max_tokens if max_tokens is not None else review_cfg.get("max_tokens", 40000)

    cleanup_fn = None
    pr_title = pr_description = ""

    if pr_url:
        from diffgraph.bitbucket import fetch_pr
        console.print(f"[bold]PR[/bold]  [cyan]{pr_url}[/cyan]")
        try:
            diff_text, repo_path, cleanup_fn, pr_meta = fetch_pr(
                pr_url,
                on_status=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            )
        except Exception as exc:
            console.print(f"[red]Failed to fetch PR: {exc}[/red]")
            raise typer.Exit(1)
        pr_title       = pr_meta.get("title", "")
        pr_description = pr_meta.get("description", "")
        if pr_title:
            console.print(f"  [dim]title: {pr_title}[/dim]")
    else:
        if not repo:
            console.print("[red]Provide --repo or --pr-url.[/red]")
            raise typer.Exit(1)
        diff_text = _read_diff(diff)
        repo_path = str(Path(repo).resolve())

    if not diff_text.strip():
        console.print("[yellow]Diff is empty — nothing to do.[/yellow]")
        if cleanup_fn:
            cleanup_fn()
        raise typer.Exit(0)

    # Show diff summary
    sys.path.insert(0, str(BASE_DIR))
    from diffgraph.diff_parser import parse_diff
    diff_result = parse_diff(diff_text)
    _print_diff_summary(diff_result)

    # Fetch existing comments if posting to PR
    existing_comments: list = []
    if pr_url:
        try:
            from diffgraph.bitbucket import get_pr_comments
            existing_comments = get_pr_comments(pr_url)
            if existing_comments:
                console.print(f"  [dim]{len(existing_comments)} existing comment(s) loaded[/dim]")
        except Exception as exc:
            console.print(f"  [yellow]Could not fetch existing comments: {exc}[/yellow]")

    console.print(
        f"\n[bold]Review[/bold]  repo=[cyan]{repo_path}[/cyan]"
        f"  model=[cyan]{effective_model}[/cyan]"
        f"  steps=[cyan]{effective_steps}[/cyan]\n"
    )

    llm_client = _make_llm_client(llm_cfg)
    from diffgraph import DiffGraph
    dg = DiffGraph(
        repo_path=repo_path,
        llm_client=llm_client,
        llm_model=effective_model,
        max_steps=effective_steps,
        max_tokens=effective_tokens,
    )

    _root_agent_ref: dict = {"agent": None}
    _prompt_info: dict = {"source": "", "hash": ""}
    from orchestra.trace import TraceCollector
    from orchestra.trace_db import TraceDBWriter
    _trace_collector = TraceCollector()
    _trace_db = TraceDBWriter()

    def _capture_event(event: str, **kw):
        if event == "orchestrator_root_agent":
            _root_agent_ref["agent"] = kw.get("agent")
        if event == "orchestrator_prompts_compiled":
            _prompt_info["source"] = kw.get("prompt_source", "")
            _prompt_info["hash"] = kw.get("prompt_hash", "")
            _trace_db.set_prompt_info(_prompt_info["source"], _prompt_info["hash"])
        _trace_collector.on_event(event, **kw)
        # Note: _trace_db gets raw events via direct EventBus subscription
        # in orchestrator.py — no need to call it here (would duplicate)

    with Live("", console=console, refresh_per_second=8, vertical_overflow="visible") as live:
        event_handler = _make_event_handler(effective_model, live)

        def _combined_handler(event: str, **kw):
            _capture_event(event, **kw)
            event_handler(event, **kw)

        # Pass git refs for VFS (PR mode only)
        _base_ref = pr_meta.get("base_ref", "") if pr_url else ""
        _source_ref = pr_meta.get("source_ref", "") if pr_url else ""

        findings, review_ctx = dg.review(
            diff_text,
            existing_comments=existing_comments,
            on_event=_combined_handler,
            trace_writer=_trace_db.on_event,
            base_ref=_base_ref,
            source_ref=_source_ref,
            prompt_resource=prompts,
        )
    console.print("")

    _print_findings(findings)

    # Finish trace DB run
    _trace_db.finish_run(
        model=effective_model,
        pr_url=pr_url or "",
        findings_count=len(findings),
        prompt_source=_prompt_info["source"],
        prompt_hash=_prompt_info["hash"],
    )
    _trace_db.close()
    console.print(f"[dim]  trace: {_trace_db.db_path} run={_trace_db.run_id}[/dim]")
    console.print(f"[dim]  view:  python cli.py trace --log  |  python cli.py serve[/dim]")

    if post_comments and pr_url:
        from diffgraph.bitbucket import post_review_comments
        comments_to_post = [_finding_to_comment(f) for f in findings]
        changed_lines = {
            path: set(fd.after_changed_lines)
            for path, fd in diff_result.files.items()
        }
        console.print(f"\n[bold]Posting[/bold]  {len(comments_to_post)} findings to PR...\n")
        posted = post_review_comments(
            pr_url, comments_to_post,
            on_status=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            changed_lines=changed_lines,
        )
        console.print(f"\n[green]Posted {posted}/{len(comments_to_post)} comments[/green]")
    elif post_comments and not pr_url:
        console.print("[yellow]--post-comments requires --pr-url[/yellow]")

    if post_comments and pr_url:
        if review_ctx.comment_replies or review_ctx.comment_resolves:
            from diffgraph.bitbucket import reply_to_pr_comment, resolve_pr_comment
            for reply in review_ctx.comment_replies:
                try:
                    reply_to_pr_comment(pr_url, reply["comment_id"], reply["text"])
                    console.print(f"  [dim]replied to #{reply['comment_id']}[/dim]")
                except Exception as exc:
                    console.print(f"  [yellow]reply #{reply['comment_id']} failed: {exc}[/yellow]")
            for cid in review_ctx.comment_resolves:
                try:
                    resolve_pr_comment(pr_url, cid)
                    console.print(f"  [dim]resolved #{cid}[/dim]")
                except Exception as exc:
                    console.print(f"  [yellow]resolve #{cid} failed: {exc}[/yellow]")

    if output:
        Path(output).write_text(
            json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=2)
        )
        console.print(f"\n[green]Findings written to {output}[/green]  ({len(findings)} findings)")

    if cleanup_fn:
        cleanup_fn()


@app.command()
def trace(
    run_id: Optional[str] = typer.Option(None, "--run", help="Specific run ID (default: last run)"),
    list_runs: bool = typer.Option(False, "--list", "-l", help="List recent runs"),
    log_mode: bool = typer.Option(False, "--log", help="Print trace to console"),
):
    """
    View traces from the CLI. Use 'serve' for the web UI.

    \b
      python cli.py trace --list       # list recent runs
      python cli.py trace --log        # print last run to console
      python cli.py trace --log --run X  # print specific run
      python cli.py serve              # web UI for browsing traces
    """
    from orchestra.trace_db import TraceDBReader, DEFAULT_DB_PATH

    if not DEFAULT_DB_PATH.exists():
        console.print("[red]No trace database found.[/red] Run a review first.")
        raise typer.Exit(1)

    reader = TraceDBReader()

    if list_runs:
        runs = reader.list_runs(limit=20)
        if not runs:
            console.print("[dim]No runs found.[/dim]")
            raise typer.Exit(0)
        from rich.table import Table
        table = Table(title="Recent Runs", box=rich_box.SIMPLE)
        table.add_column("Run ID", style="cyan")
        table.add_column("Started")
        table.add_column("Model")
        table.add_column("Findings", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Status")
        for r in runs:
            started = r["started_at"][:19] if r["started_at"] else "?"
            table.add_row(
                r["id"],
                started,
                r["model"] or "?",
                str(r["findings_count"] or 0),
                str(r["total_tokens_paid"] or 0),
                r["status"] or "?",
            )
        console.print(table)
        reader.close()
        raise typer.Exit(0)

    # Default: --log mode
    target_id = run_id
    if not target_id:
        target_id = reader.get_last_run_id()
        if not target_id:
            console.print("[red]No runs found.[/red]")
            reader.close()
            raise typer.Exit(1)

    trace_data = reader.get_run_trace(target_id)
    reader.close()

    _print_trace_log(trace_data, depth=0)


@app.command()
def serve(
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
):
    """
    Start the trace web server for browsing review traces.

    \b
      python cli.py serve              # http://localhost:8080
      python cli.py serve --port 9000  # custom port
      python cli.py serve --host 0.0.0.0  # accessible from network
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn not installed.[/red] Run: pip install uvicorn fastapi jinja2")
        raise typer.Exit(1)

    from orchestra.trace_server.app import create_app

    console.print(f"[bold green]Trace server[/bold green] starting on http://{host}:{port}")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    import webbrowser
    webbrowser.open(f"http://{host}:{port}")

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def _print_trace_log(trace: dict, depth: int = 0):
    """Print trace to console in a log-like format."""
    import json as _json
    indent = "  " * depth
    name = trace.get("agent_name", "?")
    steps = trace.get("steps", 0)
    paid = trace.get("tokens_paid", 0)
    agent_id = trace.get("agent_id", "?")

    # Agent header
    _cc = {"low": "red", "medium": "yellow", "high": "green"}
    sgr = trace.get("sgr", [])
    conf_trail = ""
    if sgr:
        confs = [e.get("confidence", "?") for e in sgr]
        conf_trail = " → ".join(confs)

    console.print(f"\n{indent}[bold cyan]{'═' * 70}[/bold cyan]")
    console.print(f"{indent}[bold cyan]{name}[/bold cyan]  [dim]{agent_id}  {steps} steps  paid:{paid} tok[/dim]")
    if conf_trail:
        console.print(f"{indent}[dim]SGR: {conf_trail}[/dim]")
    console.print(f"{indent}[bold cyan]{'─' * 70}[/bold cyan]")

    # Interleave LLM calls and SGR by step
    llm_calls = trace.get("llm_calls", [])
    sgr_by_step = {e["step"]: e for e in sgr}
    all_steps = sorted(set(
        [c["step"] for c in llm_calls] +
        [e["step"] for e in sgr]
    ))

    # Build step data: pair each response with tool results from next request
    steps_data: list[dict] = []
    for step_num in all_steps:
        step_calls = [c for c in llm_calls if c["step"] == step_num]
        req = next((c for c in step_calls if c["type"] == "request"), None)
        resp = next((c for c in step_calls if c["type"] == "response"), None)
        steps_data.append({"step": step_num, "req": req, "resp": resp})

    # Compute per-step delta paid
    prev_paid = 0
    for sd in steps_data:
        resp = sd.get("resp")
        if resp:
            usage = resp.get("usage", {})
            current_paid = usage.get("paid", 0)
            usage["step_paid"] = current_paid - prev_paid if current_paid > prev_paid else current_paid
            prev_paid = current_paid

    # Track how many tool messages we've seen so far
    prev_tool_count = 0
    for i, sd in enumerate(steps_data):
        step_num = sd["step"]
        resp = sd["resp"]
        # Get only NEW tool results: compare tool messages in next request vs current
        tool_results = []
        if i + 1 < len(steps_data) and steps_data[i + 1]["req"]:
            next_msgs = steps_data[i + 1]["req"].get("messages", [])
            all_tool_msgs = [m.get("content", "") for m in next_msgs if m.get("role") == "tool"]
            # Only take tool messages beyond what we saw last step
            tool_results = all_tool_msgs[prev_tool_count:]
            prev_tool_count = len(all_tool_msgs)

        if resp or sd["req"]:
            _print_llm_call_log(sd["req"], resp, step_num, indent, tool_results)

        if step_num in sgr_by_step:
            _print_sgr_log(sgr_by_step[step_num], indent)

    # Children
    children = trace.get("children", [])
    if children:
        console.print(f"\n{indent}[dim italic]  spawned {len(children)} agent(s)[/dim italic]")
        for child in children:
            _print_trace_log(child, depth + 1)

    # Output
    output = trace.get("output")
    if output:
        console.print(f"\n{indent}[bold]  Output:[/bold]")
        if isinstance(output, list):
            for f in output:
                if isinstance(f, dict):
                    sev = f.get("severity", "?")
                    title = f.get("title", "?")
                    file = f.get("file", "")
                    line = f.get("line", "")
                    sev_color = {"BLOCKER": "red", "MAJOR": "yellow", "MINOR": "cyan", "COMMENT": "dim"}.get(sev, "white")
                    console.print(f"{indent}    [{sev_color}]{sev}[/{sev_color}] {title}  [dim]{file}:{line}[/dim]")
        else:
            text = _json.dumps(output, indent=2, default=str)[:500]
            console.print(f"{indent}    [dim]{text}[/dim]")


def _print_llm_call_log(req: dict | None, resp: dict | None, step: int, indent: str,
                        tool_results: list[str] | None = None):
    """Print one LLM step: call + result, no message history."""
    tool_results = tool_results or []

    # Extract info from response
    tool_names = ""
    usage_str = ""
    if resp:
        calls = resp.get("tool_calls", [])
        if calls:
            tool_names = ", ".join(c.get("name", "?") for c in calls[:4])
        usage = resp.get("usage", {})
        step_paid = usage.get("step_paid", usage.get("paid", 0))
        cached = usage.get("cached_tokens", 0)
        tok_in = usage.get("prompt_tokens", 0)
        tok_out = usage.get("completion_tokens", 0)
        if step_paid:
            usage_str = f"paid:{step_paid}"
            if cached:
                usage_str += f" (↑{tok_in} cache:{cached} ↓{tok_out})"

    model = ""
    temp = ""
    if req:
        params = req.get("llm_params", {})
        model = params.get("model", "")
        temp = f"t={params.get('temperature', '?')}"

    # Summary line
    console.print(
        f"{indent}  [dim]step {step}[/dim]  "
        f"[cyan]{tool_names or '…'}[/cyan]  "
        f"[dim]{usage_str}  {model} {temp}[/dim]"
    )

    # Call: what the LLM decided (tool calls with args)
    if resp:
        for tc in resp.get("tool_calls", []):
            args = tc.get("arguments", "")
            if isinstance(args, str) and len(args) > 200:
                args = args[:200] + "…"
            console.print(f"{indent}    [cyan]→ {tc.get('name', '?')}[/cyan]([dim]{args}[/dim])")

    # Result: what the tool returned
    for i, result in enumerate(tool_results):
        preview = result[:250].replace("\n", "↵ ")
        if len(result) > 250:
            preview += "…"
        console.print(f"{indent}    [dim]← {preview}[/dim]")


def _print_sgr_log(entry: dict, indent: str):
    """Print one SGR reflect to console."""
    conf = entry.get("confidence", "?")
    step = entry.get("step", "?")
    learned = entry.get("learned", "")
    questions = entry.get("questions_remaining", [])
    resolved = entry.get("resolved_questions", [])
    conf_color = {"low": "red", "medium": "yellow", "high": "green"}.get(conf, "white")

    console.print(f"{indent}  [bold]reflect[/bold] step {step}  conf=[{conf_color}]{conf}[/{conf_color}]")

    if learned:
        short = learned[:150].replace("\n", " ")
        if len(learned) > 150:
            short += "…"
        console.print(f"{indent}    [dim]learned: {short}[/dim]")

    for rq in resolved:
        if not isinstance(rq, dict):
            continue
        qid = rq.get("id", rq.get("question", "?"))
        res = rq.get("resolution", "?")
        summary = rq.get("summary", "")
        icon = "✓" if res == "answered" else "✗"
        color = "green" if res == "answered" else "dim"
        short_s = (summary[:80] + "…") if len(summary) > 80 else summary
        console.print(f"{indent}    [{color}]{icon} {qid}[/{color}] [dim]{short_s}[/dim]")

    for q in questions:
        if isinstance(q, dict):
            qid = q.get("id", "?")
            text = q.get("text", "")
            console.print(f"{indent}    [yellow]● {qid}: {text[:80]}[/yellow]")
        else:
            console.print(f"{indent}    [yellow]● {q}[/yellow]")


@app.command()
def inspect(
    diff: Optional[str] = typer.Argument(None, help="Path to .diff file, or '-' for stdin"),
):
    """
    Parse a diff and show what changed — no LLM required.

    \b
      python cli.py inspect changes.diff
      git diff HEAD~1 | python cli.py inspect -
    """
    diff_text = _read_diff(diff)
    if not diff_text.strip():
        console.print("[yellow]Diff is empty.[/yellow]")
        raise typer.Exit(0)

    sys.path.insert(0, str(BASE_DIR))
    from diffgraph.diff_parser import parse_diff

    result = parse_diff(diff_text)
    _print_diff_summary(result, verbose=True)


# ── display helpers ───────────────────────────────────────────────────────────

def _print_diff_summary(diff_result, verbose: bool = False) -> None:
    table = Table(title="Diff summary", show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Status")
    table.add_column("Changed lines", justify="right")
    table.add_column("Hunks", justify="right")

    for fd in diff_result.files.values():
        color = {"modified": "yellow", "added": "green",
                 "deleted": "red", "renamed": "cyan"}.get(fd.status, "white")
        table.add_row(
            fd.path,
            f"[{color}]{fd.status}[/{color}]",
            str(len(fd.after_changed_lines)),
            str(len(fd.hunks)),
        )
    console.print(table)

    if verbose:
        for fd in diff_result.files.values():
            if not fd.hunks:
                continue
            console.print(f"\n[bold]{fd.path}[/bold]")
            for i, hunk in enumerate(fd.hunks, 1):
                console.print(
                    f"  hunk {i}: before_start={hunk.before_start}  after_start={hunk.after_start}"
                    f"  -{len(hunk.before_lines)} lines  +{len(hunk.after_lines)} lines"
                )


def _print_findings(findings) -> None:
    sev_color = {"BLOCKER": "red", "MAJOR": "yellow", "MINOR": "cyan", "COMMENT": "dim"}
    if not findings:
        console.print("[dim]No findings.[/dim]")
        return
    table = Table(title=f"Findings ({len(findings)})", show_header=True, header_style="bold")
    table.add_column("Sev", width=8)
    table.add_column("File")
    table.add_column("Line", justify="right", width=6)
    table.add_column("Title")
    for f in findings:
        color = sev_color.get(f.severity, "white")
        table.add_row(
            f"[{color}]{f.severity}[/{color}]", f.file, str(f.line), f.title,
        )
    console.print(table)

    for f in findings:
        color = sev_color.get(f.severity, "white")
        console.print(
            f"\n[{color}][{f.severity}][/{color}] [bold]{f.file}:{f.line}[/bold]  {f.title}"
        )
        console.print(f"  [dim]{f.explanation}[/dim]")
        if f.evidence:
            console.print(f"  Evidence: [italic]{f.evidence[:200]}[/italic]")
        if f.suggestion:
            console.print(f"  Suggestion: {f.suggestion[:200]}")


def _finding_to_comment(finding):
    class _C:
        pass
    c = _C()
    c.file = finding.file
    c.line = finding.line
    c.severity = finding.severity
    body = f"**{finding.title}**\n\n{finding.explanation}"
    if finding.evidence:
        body += f"\n\n*Evidence:* {finding.evidence}"
    c.comment = body
    c.suggestion = finding.suggestion or None
    return c


def _make_event_handler(model: str, live: Optional[Live]):
    """
    Returns an on_event callback that renders:
      - Permanent log: compiled agents, plan panel, final SGR summary, findings
      - Live frame: actions list + SGR status, updated in-place at bottom of screen
    """

    # ── State ─────────────────────────────────────────────────────────────

    # Agent identity
    _active: dict = {"agent_id": "", "agent_name": "agent"}
    _root_id: dict = {"val": ""}  # first agent = root, only show root events
    _child_ids: set = set()       # IDs of spawned children (suppressed)

    # Action log (accumulated, shown in live frame)
    _actions: list[str] = []
    _current_stream: dict = {"text": ""}

    # SGR state — keyed by question ID
    _sgr: dict = {
        "questions":     {},   # id -> {text, age, step_opened}
        "conf_history":  [],   # [(step, conf), ...]
        "resolved_set":  set(),# resolved IDs
        "resolutions":   [],   # [(step, id, text, resolution, summary), ...]
    }

    # Budget tracking
    _budget: dict = {"step": 0, "max_steps": 40, "tok_in": 0, "tok_out": 0, "tok_cached": 0}

    # ── Render the live frame (SGR top, Actions bottom) ─────────────────────

    _cc = {"low": "red", "medium": "yellow", "high": "green"}

    def _render_sgr_section(body: Text) -> None:
        """Append SGR section to body."""
        if not _sgr["conf_history"]:
            return

        body.append("SGR · ", style="dim bold")
        for i, (_, conf) in enumerate(_sgr["conf_history"]):
            if i:
                body.append(" → ", style="dim")
            body.append(conf, style=_cc.get(conf, "white"))
        body.append("\n")

        # Resolved (compact: icon + ID + text + answer)
        if _sgr["resolutions"]:
            for entry in _sgr["resolutions"][-6:]:
                _, qid, q_text, res_type, summary = entry
                icon = "✓" if res_type == "answered" else "✗"
                color = "green" if res_type == "answered" else "dim"
                label = f"{qid}: {q_text}" if qid != q_text else q_text
                display_q = (label[:36] + "…") if len(label) > 38 else label
                body.append(f"  {icon} ", style=f"bold {color}")
                body.append(display_q, style="dim")
                if summary and summary != "(implicit)":
                    display_s = (summary[:55] + "…") if len(summary) > 57 else summary
                    body.append(f" → {display_s}", style="dim italic")
                body.append("\n")

        # Open questions (keyed by ID, value is {text, age, step_opened})
        if _sgr["questions"]:
            body.append(f"\n  Open ({len(_sgr['questions'])})\n", style="bold")
            for qid, qdata in _sgr["questions"].items():
                text = qdata["text"] if isinstance(qdata, dict) else str(qdata)
                age = qdata["age"] if isinstance(qdata, dict) else 0
                step_opened = qdata.get("step_opened", "?") if isinstance(qdata, dict) else "?"
                if age == 0:
                    dot, color, tag = "●", "green", "  new"
                elif age == 1:
                    dot, color, tag = "●", "yellow", ""
                else:
                    dot, color, tag = "●", "red", "  ⚠"
                label = f"{qid}: {text}" if qid != text else text
                display_q = (label[:56] + "…") if len(label) > 58 else label
                body.append(f"    {dot} ", style=f"bold {color}")
                body.append(display_q, style="")
                body.append(f"  step {step_opened}{tag}\n", style="dim")

    def _render_live_stream() -> Text:
        """Minimal live indicator: just the current stream line."""
        agent_name = _active["agent_name"]
        step = _budget["step"]
        stream = _current_stream["text"]
        if stream:
            return Text.assemble(
                (f"  {agent_name} ", "dim cyan"),
                (stream, "dim"),
            )
        elif _actions:
            # Show last action if no active stream
            last = _actions[-1]
            try:
                return Text.from_markup(f"  [dim cyan]{agent_name}[/dim cyan] [dim]{last}[/dim]")
            except Exception:
                return Text(f"  {agent_name} {last}", style="dim")
        return Text(f"  {agent_name} · step {step}…", style="dim")

    def _update_live() -> None:
        """Update live indicator with minimal text."""
        if not live:
            return
        live.update(_render_live_stream())

    def _update_stream_only() -> None:
        """Throttled stream update."""
        if not live:
            return
        now = _time.monotonic()
        if now - _last_stream_update["t"] < 0.2:
            return
        _last_stream_update["t"] = now
        live.update(_render_live_stream())

    # ── Render final summary (SGR + compact actions, logged permanently) ────

    def _render_final_summary() -> Panel:
        body = Text()

        # SGR section (compact — all resolutions, no open questions section)
        if _sgr["conf_history"]:
            body.append("SGR · ", style="dim bold")
            for i, (_, conf) in enumerate(_sgr["conf_history"]):
                if i:
                    body.append(" → ", style="dim")
                body.append(conf, style=_cc.get(conf, "white"))
            body.append("\n")

            for entry in _sgr["resolutions"]:
                _, qid, q_text, res_type, summary = entry
                icon = "✓" if res_type == "answered" else "✗"
                color = "green" if res_type == "answered" else "dim"
                label = f"{qid}: {q_text}" if qid != q_text else q_text
                display_q = (label[:36] + "…") if len(label) > 38 else label
                body.append(f"  {icon} ", style=f"bold {color}")
                body.append(display_q, style="dim" if res_type != "answered" else "")
                if summary and summary != "(implicit)":
                    display_s = (summary[:55] + "…") if len(summary) > 57 else summary
                    body.append(f" → {display_s}", style="dim italic")
                elif res_type != "answered":
                    body.append(" → (dropped)", style="dim italic")
                body.append("\n")

            if _sgr["questions"]:
                for qid, qdata in _sgr["questions"].items():
                    text = qdata["text"] if isinstance(qdata, dict) else str(qdata)
                    label = f"{qid}: {text}" if qid != text else text
                    display_q = (label[:56] + "…") if len(label) > 58 else label
                    body.append("  ● ", style="bold yellow")
                    body.append(f"{display_q} (open)\n", style="yellow")

            body.append("\n")

        # Compact actions (no tokens, just tool + short args)
        for line in _actions:
            # Strip Rich markup for compact log
            import re as _re
            clean = _re.sub(r"\[/?[^\]]*\]", "", line)
            # Remove token info (↑... ↓...)
            clean = _re.sub(r"\s+[↑↓][^\s]*", "", clean).strip()
            body.append(f"  {clean}\n", style="dim")

        step = _budget["step"]
        tok_in = _budget["tok_in"]
        tok_out = _budget["tok_out"]
        return Panel(
            body,
            title=f"[dim]{_active['agent_name']} · done · {step} steps · ↑{tok_in} ↓{tok_out}[/dim]",
            border_style="dim",
            box=rich_box.ROUNDED,
            padding=(0, 1),
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _log(msg: str) -> None:
        (live.console if live else console).log(msg)

    def _fmt_tok_short() -> str:
        tok_in = _budget["tok_in"]
        tok_out = _budget["tok_out"]
        tok_cached = _budget["tok_cached"]
        if not (tok_in or tok_out):
            return ""
        if tok_cached:
            # Show paid tokens: uncached_in + cached*0.1 + out
            paid_in = (tok_in - tok_cached) + int(tok_cached * 0.1)
            return f"↑{tok_in}[cache:{tok_cached}] ↓{tok_out} paid:{paid_in + tok_out}"
        return f"↑{tok_in} ↓{tok_out}"

    # ── Event handler ─────────────────────────────────────────────────────

    def _print_and_reset(agent_id: str) -> None:
        """Print final summary for current agent and reset state."""
        if not _sgr["conf_history"] and not _actions:
            return  # nothing to print
        if live:
            live.update("")
        if _sgr["conf_history"] or _actions:
            (live.console if live else console).print(_render_final_summary())
        # Reset state
        _sgr["questions"].clear()
        _sgr["conf_history"].clear()
        _sgr["resolved_set"].clear()
        _sgr["resolutions"].clear()
        _actions.clear()
        _current_stream["text"] = ""
        _budget.update(step=0, tok_in=0, tok_out=0, tok_cached=0)

    def _switch_agent(agent_id: str, agent_name: str) -> None:
        """Switch live frame to a new agent. Does NOT print final summary."""
        _active["agent_id"] = agent_id
        _active["agent_name"] = agent_name

    def on_event(event: str, **kw) -> None:
        aid = kw.get("agent_id", "")
        aname = kw.get("agent_name", "")

        # Track root agent (first agent that starts)
        if event == "orchestrator_agent_started" and not _root_id["val"]:
            _root_id["val"] = aid

        # Register children
        if event == "orchestrator_agent_spawned":
            child_id = kw.get("child_id", "")
            if child_id:
                _child_ids.add(child_id)

        # Suppress ALL events from child agents — they work silently.
        # Their results appear in root's spawn_agent/spawn_many tool result.
        is_child = aid in _child_ids
        if is_child:
            return

        # Update active agent for root events
        if aid and aid == _root_id["val"] and aid != _active["agent_id"]:
            _switch_agent(aid, aname or aid[:8])

        if event == "orchestrator_agent_compiled":
            name = kw.get("name", "?")
            mode = kw.get("mode", "?")
            caps = kw.get("capabilities", "–")
            data = kw.get("data", "–")
            bt = kw.get("budget_tokens", 0)
            bs = kw.get("budget_steps", 0)
            _budget["max_steps"] = max(_budget["max_steps"], bs)
            _log(f"[dim]  compiled [cyan]{name}[/cyan] \\[{mode}]  caps=\\[{caps}]  data=\\[{data}]  budget={bt}t/{bs}s[/dim]")

        elif event == "orchestrator_agent_started":
            pass

        elif event == "orchestrator_plan_start":
            _log("[bold green]plan[/bold green]      lead analyzing diff…")

        elif event == "orchestrator_agent_spawned":
            child = kw.get("child_id", "?")
            name = kw.get("agent_name", "?") or kw.get("name", "?")
            focus = kw.get("focus", "")
            focus_short = (focus[:60] + "…") if len(focus) > 62 else focus
            if focus_short:
                _actions.append(f"[bold cyan]spawn {name}[/bold cyan] → {focus_short}")
            else:
                _actions.append(f"[bold cyan]spawn {name}[/bold cyan] ({child[:6]})")
            _update_live()

        elif event == "orchestrator_agent_done":
            # Print final summary for the finishing agent
            _print_and_reset(kw.get("agent_id", ""))

        elif event == "orchestrator_plan_done":
            plan = kw.get("plan", {})
            system_type = plan.get("system_type", "?")
            tasks = plan.get("tasks", [])
            pri_color = {"high": "red", "medium": "yellow", "low": "green"}
            body = Text()
            for t in tasks:
                pri = t.get("priority", "?")
                color = pri_color.get(pri, "white")
                body.append(f"\n  [{pri}] ", style=f"bold {color}")
                body.append(t.get("type", "?"), style="bold")
                body.append("\n")
                focus = t.get("focus", "")
                if focus:
                    body.append(f"    {focus}\n", style="")
                hints = t.get("search_hints") or []
                if hints:
                    body.append(f"    → {' · '.join(hints)}\n", style="dim")
            panel = Panel(
                body,
                title=f"[bold green]plan[/bold green] · [cyan]{system_type}[/cyan] · {len(tasks)} focuses",
                border_style="dim", box=rich_box.ROUNDED, padding=(0, 1),
            )
            (live.console if live else console).print(panel)

        elif event == "orchestrator_stream":
            tool_name = kw.get("tool_name", "")
            args_preview = kw.get("args_preview", "")
            tok = kw.get("tok", 0)
            step = kw.get("step", 0)
            _current_stream["text"] = f"step {step}  {tool_name or '…'}({args_preview})  ↓{tok}…"
            _update_stream_only()

        elif event == "orchestrator_step":
            tool = kw.get("tool", "")
            step = kw.get("step", 0)
            _budget["step"] = step + 1
            _budget["tok_in"] = kw.get("tok_in", _budget["tok_in"])
            _budget["tok_out"] = kw.get("tok_out", _budget["tok_out"])
            _budget["tok_cached"] = kw.get("tok_cached", _budget["tok_cached"])

        elif event == "orchestrator_result":
            step = kw.get("step", 0)
            tool = kw.get("tool", "")
            args = kw.get("args", {})
            result_preview = kw.get("result_preview", "")
            tok_str = _fmt_tok_short()

            # Build compact args string
            if tool == "spawn_agent":
                agent = args.get("agent", "?")
                data = args.get("data", {})
                focus = data.get("focus", args.get("focus", ""))
                focus_short = (focus[:50] + "…") if len(focus) > 52 else focus
                arg_str = f"[cyan]{agent}[/cyan]"
                if focus_short:
                    arg_str += f" → {focus_short}"
            elif tool == "spawn_many":
                agents = args.get("agents", [])
                names = [a.get("agent", "?") for a in agents[:4]]
                arg_str = f"[cyan]{', '.join(names)}[/cyan] ×{len(agents)}"
            elif tool in ("reply_to_comment", "resolve_comment"):
                cid = args.get("comment_id", "?")
                text = args.get("text", "")
                text_short = (text[:40] + "…") if len(text) > 42 else text
                arg_str = f"#{cid}"
                if text_short:
                    arg_str += f" {text_short}"
            elif tool == "get_diff":
                path = args.get("path", "")
                arg_str = path if path else "(full)"
            else:
                # Generic: show first 1-2 args compactly
                parts = []
                for k, v in list(args.items())[:2]:
                    vs = str(v)
                    if len(vs) > 30:
                        vs = vs[:28] + "…"
                    parts.append(f"{k}={vs}")
                arg_str = ", ".join(parts)

            # Build result preview for meta-tools
            res_str = ""
            if tool in ("spawn_agent", "spawn_many") and result_preview:
                # Show first part of output
                preview = result_preview[:80].replace("{", "").replace("}", "").replace('"', "")
                res_str = f" [dim]→ {preview}[/dim]"

            action_line = f"step {step}  {tool}"
            if arg_str:
                action_line += f"({arg_str})"
            action_line += f"  {tok_str}{res_str}"
            _actions.append(action_line)
            _current_stream["text"] = ""
            _update_live()

        elif event == "orchestrator_reflect":
            step = kw.get("step", 0)
            conf = kw.get("confidence", "?")
            questions = kw.get("questions_remaining", [])
            resolved_questions = kw.get("resolved_questions", [])

            # Process resolved questions
            for rq in (resolved_questions or []):
                if not isinstance(rq, dict):
                    continue
                qid = rq.get("id", "") or rq.get("question", "")
                q_text = rq.get("question", "") or rq.get("text", "") or qid
                res_type = rq.get("resolution", "answered")
                summary = rq.get("summary", "")
                if qid and qid not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(qid)
                    # Use stored text if available (more stable)
                    stored = _sgr["questions"].get(qid, {})
                    display_text = stored.get("text", q_text) if isinstance(stored, dict) else q_text
                    _sgr["resolutions"].append((step, qid, display_text, res_type, summary))

            # Extract IDs from remaining questions
            new_q: dict[str, str] = {}  # id -> text
            for q in questions:
                if isinstance(q, dict):
                    qid = q.get("id", q.get("text", ""))
                    text = q.get("text", qid)
                    new_q[qid] = text
                else:
                    new_q[str(q)] = str(q)

            # Detect implicit drops (ID disappeared without resolution)
            for qid in list(_sgr["questions"].keys()):
                if qid not in new_q and qid not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(qid)
                    old = _sgr["questions"][qid]
                    old_text = old.get("text", qid) if isinstance(old, dict) else str(old)
                    _sgr["resolutions"].append((step, qid, old_text, "dropped", "(implicit)"))

            # PUT semantics: update existing by ID, add new
            new_questions: dict[str, dict] = {}
            for qid, text in new_q.items():
                if qid in _sgr["questions"]:
                    old = _sgr["questions"][qid]
                    age = old["age"] + 1 if isinstance(old, dict) else 1
                    step_opened = old.get("step_opened", step) if isinstance(old, dict) else step
                    new_questions[qid] = {"text": text, "age": age, "step_opened": step_opened}
                else:
                    new_questions[qid] = {"text": text, "age": 0, "step_opened": step}
            _sgr["questions"] = new_questions
            _sgr["conf_history"].append((step, conf))

            # Add reflect to action log
            conf_color = {"low": "red", "medium": "yellow", "high": "green"}.get(conf, "white")
            _actions.append(f"step {step}  reflect()  conf=[{conf_color}]{conf}[/{conf_color}]")
            _current_stream["text"] = ""
            _update_live()

        elif event == "orchestrator_done":
            if live:
                live.update("")
            _log(
                f"[bold green]done[/bold green]      "
                f"[dim]{kw.get('findings', 0)} finding(s)  "
                f"{kw.get('replies', 0)} replies  "
                f"{kw.get('resolves', 0)} resolves[/dim]"
            )

        elif event == "orchestrator_forced_done":
            tok_str = _fmt_tok_short()
            _log(
                f"[yellow]forced[/yellow]    {kw.get('reason', 'limit')}  "
                f"[dim cyan]{tok_str}[/dim cyan]"
            )
            # Print final summary (if not already printed by agent_done)
            if _sgr["conf_history"] or _actions:
                _print_and_reset(kw.get("agent_id", ""))

    return on_event


def _fmt_tok(kw: dict) -> str:
    tok_in = kw.get("tok_in", 0)
    tok_out = kw.get("tok_out", 0)
    tok_cached = kw.get("tok_cached", 0)
    if not (tok_in or tok_out):
        return ""
    in_str = f"↑{tok_in}[{tok_cached}]" if tok_cached else f"↑{tok_in}"
    return f"{in_str} ↓{tok_out}"


if __name__ == "__main__":
    app()
