#!/usr/bin/env python3
from __future__ import annotations

# Use OS trust store for SSL — picks up corporate VPN/proxy CA certificates
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import os
# Force UTF-8 mode on Windows (avoids cp1251 crashes in subprocess)
os.environ["PYTHONUTF8"] = "1"

import json
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box as rich_box
from rich.console import Console
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
    # Timeout
    timeout = llm_cfg.get("timeout", 600)
    kwargs["timeout"] = timeout

    # Custom CA bundle or SSL disable for LLM endpoint
    ca_bundle = llm_cfg.get("ca_bundle", "").strip() or os.environ.get("LLM_CA_BUNDLE", "")
    if ca_bundle:
        ca_bundle = os.path.expanduser(ca_bundle)
    no_ssl = os.environ.get("GIT_SSL_NO_VERIFY") == "1"  # set by --no-verify-ssl
    if no_ssl:
        import httpx
        kwargs["http_client"] = httpx.Client(verify=False, timeout=timeout)
    elif ca_bundle:
        import httpx
        kwargs["http_client"] = httpx.Client(verify=ca_bundle, timeout=timeout)
    return OpenAI(**kwargs)


def _run_dispatcher(
    pr_url: str,
    command: str,
    cmd_args: str,
    comment_id: int,
    llm_cfg: dict,
    effective_model: str,
    prompts: Optional[str],
) -> None:
    """
    Run the dispatcher agent. If it handles the request (help, unsupported
    command, etc.), posts replies and exits. If it returns action="review",
    returns normally so the caller can proceed with the review pipeline.
    """
    from diffgraph.bitbucket import (
        get_comment_thread, reply_to_pr_comment, parse_pr_url,
    )
    from diffgraph.orchestrator import run_agent
    from orchestra import ToolRegistry

    # Fetch lightweight context (no clone needed)
    thread = "(no thread)"
    if comment_id:
        try:
            thread = get_comment_thread(pr_url, comment_id)
        except Exception as exc:
            thread = f"(failed to fetch thread: {exc})"

    # PR title/description from API
    pr_title = pr_description = ""
    try:
        from diffgraph.bitbucket import get_pr_info
        info = get_pr_info(pr_url)
        pr_title = info.get("title", "")
        pr_description = info.get("description", "")
    except Exception:
        pass

    # Prompt generation/mutation from compiled prompts
    from orchestra import compile_prompts
    from pathlib import Path as _Path
    prompt_source = prompts or str(_Path(__file__).parent / "diffgraph" / "prompts")
    try:
        registry = compile_prompts(prompt_source, pattern="*.prompt")
        generation = str(prompt_source).rsplit("/", 1)[-1] if "/" in str(prompt_source) else str(prompt_source)
        mutation = registry.source_hash[:7] if registry.source_hash else "unknown"
    except Exception:
        generation = "default"
        mutation = "unknown"

    data = {
        "command": command,
        "args": cmd_args,
        "comment_id": str(comment_id),
        "comment_thread": thread,
        "pr_title": pr_title,
        "pr_description": pr_description or "(no description)",
        "generation": generation,
        "mutation": mutation,
    }

    # Build tool registry with reply_to_comment
    tool_registry = ToolRegistry()
    _replies: list[dict] = []

    @tool_registry.register(
        name="reply_to_comment",
        description="Reply to a PR comment thread.",
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["comment_id", "text"],
        },
    )
    def _reply(comment_id: int = 0, text: str = "") -> dict:
        _replies.append({"comment_id": comment_id, "text": text})
        return {"status": "queued"}

    llm_client = _make_llm_client(llm_cfg)
    console.print(f"[dim]  dispatcher: /{command} {cmd_args}[/dim]")

    event_handler = _make_event_handler(effective_model)
    result = run_agent(
        agent_name="dispatcher",
        data=data,
        llm=llm_client,
        model=effective_model,
        tool_registry=tool_registry,
        on_event=event_handler,
        prompt_resource=prompts,
        tool_choice=llm_cfg.get("tool_choice", ""),
    )

    # Post queued replies
    if _replies and pr_url:
        for reply in _replies:
            try:
                reply_to_pr_comment(pr_url, reply["comment_id"], reply["text"])
                console.print(f"  [dim]replied to #{reply['comment_id']}[/dim]")
            except Exception as exc:
                console.print(f"  [yellow]reply #{reply['comment_id']} failed: {exc}[/yellow]")

    # Check if dispatcher wants to proceed with review
    action = result.get("action", "")
    if action == "review":
        return  # caller will proceed with review pipeline

    # Dispatcher handled it — exit
    raise typer.Exit(0)


# ── commands ──────────────────────────────────────────────────────────────────

@app.command()
def run(
    pr_url:        Optional[str] = typer.Option(None,  "--pr-url",             help="Bitbucket Server PR URL — clones repo and fetches diff automatically"),
    repo:          Optional[str] = typer.Option(None,  "--repo",         "-r", help="Path to local repository"),
    base:          Optional[str] = typer.Option(None,  "--base",               help="Base ref (commit/branch to merge into). Required with --repo."),
    source:        Optional[str] = typer.Option(None,  "--source",             help="Source ref (commit/branch being reviewed). Default: HEAD."),
    model:         Optional[str] = typer.Option(None,  "--model",        "-m", help="LLM model override"),
    api_url:       Optional[str] = typer.Option(None,  "--api-url",            help="OpenAI-compatible API base URL override"),
    api_key:       Optional[str] = typer.Option(None,  "--api-key",            help="API key override"),
    output:        Optional[str] = typer.Option(None,  "--output",       "-o", help="Write findings as JSON to file"),
    post_comments: bool          = typer.Option(False, "--post-comments",      help="Post findings to the PR as inline comments (requires --pr-url)"),
    max_steps:     Optional[int] = typer.Option(None,  "--max-steps",          help="Max ReAct steps (default: from config)"),
    max_tokens:    Optional[int] = typer.Option(None,  "--max-tokens",         help="Max token budget (default: from config)"),
    prompts:       Optional[str] = typer.Option(None,  "--prompts",            help="Prompt resource URI (path, file://, bitbucket://)"),
    log_level:     Optional[str] = typer.Option(None,  "--log-level",          help="Logging level: DEBUG, INFO, WARNING, ERROR"),
    verbose:       bool          = typer.Option(False, "--verbose", "-v",      help="Shortcut for --log-level DEBUG (shows HTTP, LLM calls)"),
    no_verify_ssl: bool          = typer.Option(False, "--no-verify-ssl",      help="Disable SSL verification for all connections (LLM + Bitbucket)"),
    command:       Optional[str] = typer.Option(None,  "--command",             help="Dispatch command (review, help, etc.). Runs dispatcher agent first."),
    args:          Optional[str] = typer.Option(None,  "--args",               help="Arguments for the command (question, topic, etc.)"),
    comment_id:    Optional[int] = typer.Option(None,  "--comment-id",         help="Bitbucket comment ID that triggered this invocation"),
):
    """
    Run a multi-agent PR review and print structured findings.

    \b
      python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42
      python cli.py run --pr-url ... --post-comments
      python cli.py run --repo . --base HEAD~1
      python cli.py run --repo . --base main --source feature/my-branch
    """
    import logging
    level = log_level or ("DEBUG" if verbose else "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # HTTP/httpx noise: only show at DEBUG level
    if level.upper() == "DEBUG":
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
    else:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Disable SSL verification globally
    if no_verify_ssl:
        import ssl
        ssl._create_default_context = lambda *a, **kw: ssl.create_default_context()
        # urllib3 / requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        # git operations
        os.environ["GIT_SSL_NO_VERIFY"] = "1"

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

    # ── Dispatcher: handle command before expensive clone ─────────────────
    if command is not None and pr_url:
        _run_dispatcher(
            pr_url=pr_url,
            command=command or "",
            cmd_args=args or "",
            comment_id=comment_id or 0,
            llm_cfg=llm_cfg,
            effective_model=effective_model,
            prompts=prompts,
        )
        # _run_dispatcher calls sys.exit if no review needed.
        # If we get here, dispatcher said action="review" — proceed.
        console.print("[dim]  dispatcher → review[/dim]")

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
        if not base:
            console.print("[red]Provide --base (e.g. --base HEAD~1 or --base main).[/red]")
            raise typer.Exit(1)
        repo_path = str(Path(repo).resolve())
        _source_ref = source or "HEAD"
        _base_ref = base
        # Resolve to SHAs
        import subprocess
        try:
            _base_ref = subprocess.run(
                ["git", "rev-parse", _base_ref], cwd=repo_path,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            _source_ref = subprocess.run(
                ["git", "rev-parse", _source_ref], cwd=repo_path,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError as exc:
            console.print(f"[red]Failed to resolve refs: {exc.stderr.strip()}[/red]")
            raise typer.Exit(1)
        # Compute diff from refs
        diff_text = subprocess.run(
            ["git", "diff", f"{_base_ref}...{_source_ref}"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout

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
        tool_choice=llm_cfg.get("tool_choice", ""),
        bot_user=review_cfg.get("bot_user", ""),
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

    event_handler = _make_event_handler(effective_model)

    def _combined_handler(event: str, **kw):
        _capture_event(event, **kw)
        event_handler(event, **kw)

    if True:

        # Refs: from PR metadata or from CLI args (already resolved above)
        if pr_url:
            _base_ref = pr_meta.get("base_ref", "")
            _source_ref = pr_meta.get("source_ref", "")

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

    # Build comment metadata tag for traceability
    _comment_meta = None
    if _prompt_info["source"] or _prompt_info["hash"]:
        src = _prompt_info["source"]
        gen = src.rstrip("/").rsplit("/", 1)[-1] if "/" in src else src
        if gen == "prompts" and "/" in src:
            gen = src.rstrip("/").rsplit("/", 2)[-2]
        _comment_meta = {
            "gen": gen,
            "hash": _prompt_info["hash"][:7] if _prompt_info["hash"] else "",
            "run": _trace_db.run_id,
        }

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
            comment_meta=_comment_meta,
        )
        console.print(f"\n[green]Posted {posted}/{len(comments_to_post)} comments[/green]")
    elif post_comments and not pr_url:
        console.print("[yellow]--post-comments requires --pr-url[/yellow]")

    if post_comments and pr_url:
        if review_ctx.comment_replies or review_ctx.comment_resolves:
            from diffgraph.bitbucket import reply_to_pr_comment, resolve_pr_comment
            for reply in review_ctx.comment_replies:
                try:
                    reply_to_pr_comment(pr_url, reply["comment_id"], reply["text"],
                                        comment_meta=_comment_meta)
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

    from tracing.server.app import create_app

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


_agent_log = __import__("logging").getLogger("diffgraph.agent")


def _make_event_handler(model: str):
    """
    Returns an on_event callback.

    All agents (root and children) go through the same formatting path.
    Only SGR tracking and summary panels apply to the root agent.
    """

    # ── State ─────────────────────────────────────────────────────────────

    _root_id: dict = {"val": ""}
    _child_ids: set = set()

    # SGR state — keyed by question ID (root agent only)
    _sgr: dict = {
        "questions":     {},   # id -> {text, age, step_opened}
        "conf_history":  [],   # [(step, conf), ...]
        "resolved_set":  set(),# resolved IDs
        "resolutions":   [],   # [(step, id, text, resolution, summary), ...]
    }

    # Budget tracking (root agent only, for summary panel)
    _budget: dict = {"step": 0, "max_steps": 40, "tok_in": 0, "tok_out": 0, "tok_cached": 0}

    _cc = {"low": "red", "medium": "yellow", "high": "green"}

    # ── Render final SGR summary panel ────────────────────────────────────

    def _render_final_summary(agent_name: str) -> Panel:
        body = Text()

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

        step = _budget["step"]
        tok_in = _budget["tok_in"]
        tok_out = _budget["tok_out"]
        return Panel(
            body,
            title=f"[dim]{agent_name} · done · {step} steps · ↑{tok_in} ↓{tok_out}[/dim]",
            border_style="dim",
            box=rich_box.ROUNDED,
            padding=(0, 1),
        )

    def _print_and_reset(agent_name: str) -> None:
        """Print final SGR summary for root agent and reset state."""
        if _sgr["conf_history"]:
            console.print(_render_final_summary(agent_name))
        _sgr["questions"].clear()
        _sgr["conf_history"].clear()
        _sgr["resolved_set"].clear()
        _sgr["resolutions"].clear()
        _budget.update(step=0, tok_in=0, tok_out=0, tok_cached=0)

    # ── Formatting helpers ────────────────────────────────────────────────

    def _format_args(tool: str, args: dict) -> str:
        """Build compact args string for a tool call."""
        if tool == "spawn_agent":
            agent = args.get("agent", "?")
            data = args.get("data", {})
            focus = data.get("focus", args.get("focus", ""))
            focus_short = (focus[:50] + "…") if len(focus) > 52 else focus
            s = agent
            if focus_short:
                s += f" → {focus_short}"
            return s
        elif tool == "spawn_many":
            agents = args.get("agents", [])
            names = [a.get("agent", "?") for a in agents[:4]]
            return f"{', '.join(names)} x{len(agents)}"
        elif tool in ("reply_to_comment", "resolve_comment"):
            cid = args.get("comment_id", "?")
            text = args.get("text", "")
            text_short = (text[:40] + "…") if len(text) > 42 else text
            s = f"#{cid}"
            if text_short:
                s += f" {text_short}"
            return s
        elif tool == "get_diff":
            path = args.get("path", "")
            return path if path else "(full)"
        else:
            parts = []
            for k, v in list(args.items())[:2]:
                vs = str(v)
                if len(vs) > 30:
                    vs = vs[:28] + "…"
                parts.append(f"{k}={vs}")
            return ", ".join(parts)

    def _format_result_count(result_preview: str, result_count) -> str:
        """Build result count suffix for log line."""
        if result_preview.startswith("(no matches") or result_preview.startswith("(no results"):
            return " → 0 results"
        elif result_count is not None:
            return f" → {result_count} lines"
        return ""

    # ── Event handler ─────────────────────────────────────────────────────

    def on_event(event: str, **kw) -> None:
        aid = kw.get("agent_id", "")
        aname = kw.get("agent_name", "") or aid[:8]
        is_root = aid == _root_id.get("val", "")

        # Track root agent (first agent that starts)
        if event == "orchestrator_agent_started" and not _root_id["val"]:
            _root_id["val"] = aid
            is_root = True
            _agent_log.info("agent started: %s (%s)", aname, aid[:8])

        # Register children
        if event == "orchestrator_agent_spawned":
            child_id = kw.get("child_id", "")
            if child_id:
                _child_ids.add(child_id)
            name = kw.get("agent_name", "?") or kw.get("name", "?")
            focus = kw.get("focus", "")
            focus_short = (focus[:60] + "…") if len(focus) > 62 else focus
            _agent_log.info("spawn %s → %s", name, focus_short or child_id[:8])

        elif event == "orchestrator_agent_compiled":
            name = kw.get("name", "?")
            mode = kw.get("mode", "?")
            caps = kw.get("capabilities", "–")
            data = kw.get("data", "–")
            bt = kw.get("budget_tokens", 0)
            bs = kw.get("budget_steps", 0)
            _budget["max_steps"] = max(_budget["max_steps"], bs)
            console.log(f"[dim]  compiled [cyan]{name}[/cyan] \\[{mode}]  caps=\\[{caps}]  data=\\[{data}]  budget={bt}t/{bs}s[/dim]")

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
            console.print(panel)

        elif event == "orchestrator_step":
            # Budget tracking (root only — used for summary panel)
            if is_root:
                _budget["step"] = kw.get("step", 0) + 1
                _budget["tok_in"] = kw.get("tok_in", _budget["tok_in"])
                _budget["tok_out"] = kw.get("tok_out", _budget["tok_out"])
                _budget["tok_cached"] = kw.get("tok_cached", _budget["tok_cached"])

        elif event == "orchestrator_result":
            tool = kw.get("tool", "")
            args = kw.get("args", {})
            result_preview = kw.get("result_preview", "")
            result_count = kw.get("result_count")
            arg_str = _format_args(tool, args)
            count_str = _format_result_count(result_preview, result_count)
            _agent_log.info("%s: %s(%s)%s", aname, tool, arg_str[:80], count_str)

        elif event == "orchestrator_reflect":
            step = kw.get("step", 0)
            conf = kw.get("confidence", "?")
            _agent_log.info("%s: reflect  %s", aname, conf)

            # SGR tracking (root only — for summary panel)
            if is_root:
                questions = kw.get("questions_remaining", [])
                resolved_questions = kw.get("resolved_questions", [])

                for rq in (resolved_questions or []):
                    if not isinstance(rq, dict):
                        continue
                    qid = rq.get("id", "") or rq.get("question", "")
                    q_text = rq.get("question", "") or rq.get("text", "") or qid
                    res_type = rq.get("resolution", "answered")
                    summary = rq.get("summary", "")
                    if qid and qid not in _sgr["resolved_set"]:
                        _sgr["resolved_set"].add(qid)
                        stored = _sgr["questions"].get(qid, {})
                        display_text = stored.get("text", q_text) if isinstance(stored, dict) else q_text
                        _sgr["resolutions"].append((step, qid, display_text, res_type, summary))

                new_q: dict[str, str] = {}
                for q in questions:
                    if isinstance(q, dict):
                        qid = q.get("id", q.get("text", ""))
                        text = q.get("text", qid)
                        new_q[qid] = text
                    else:
                        new_q[str(q)] = str(q)

                for qid in list(_sgr["questions"].keys()):
                    if qid not in new_q and qid not in _sgr["resolved_set"]:
                        _sgr["resolved_set"].add(qid)
                        old = _sgr["questions"][qid]
                        old_text = old.get("text", qid) if isinstance(old, dict) else str(old)
                        _sgr["resolutions"].append((step, qid, old_text, "dropped", "(implicit)"))

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

        elif event == "orchestrator_agent_done":
            _agent_log.info("%s: done", aname)
            if is_root:
                _print_and_reset(aname)

        elif event == "orchestrator_forced_done":
            _agent_log.info("%s: forced done (%s)", aname, kw.get("reason", "limit"))
            if is_root:
                _print_and_reset(aname)

        elif event == "orchestrator_done":
            _agent_log.info("done: %d findings, %d replies, %d resolves",
                            kw.get("findings", 0), kw.get("replies", 0), kw.get("resolves", 0))

    return on_event


if __name__ == "__main__":
    app()
