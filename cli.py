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

    with Live("", console=console, refresh_per_second=8, vertical_overflow="visible") as live:
        findings, review_ctx = dg.review(
            diff_text,
            existing_comments=existing_comments,
            on_event=_make_event_handler(effective_model, live),
        )
    console.print("")

    _print_findings(findings)

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

        # Apply queued replies/resolves
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

    elif post_comments and not pr_url:
        console.print("[yellow]--post-comments requires --pr-url[/yellow]")

    if output:
        Path(output).write_text(
            json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=2)
        )
        console.print(f"\n[green]Findings written to {output}[/green]  ({len(findings)} findings)")

    if cleanup_fn:
        cleanup_fn()


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

    # Agent identity (tracks which agent is currently displayed)
    _active: dict = {"agent_id": "", "agent_name": "agent"}

    # Action log (accumulated, shown in live frame)
    _actions: list[str] = []
    _current_stream: dict = {"text": ""}

    # SGR state
    _sgr: dict = {
        "questions":     {},
        "step_opened":   {},
        "conf_history":  [],
        "resolved_set":  set(),
        "resolutions":   [],
    }

    # Budget tracking
    _budget: dict = {"step": 0, "max_steps": 40, "tok_in": 0, "tok_out": 0, "tok_cached": 0}

    # ── Render the live frame ─────────────────────────────────────────────

    def _render_live_frame() -> Panel:
        body = Text()

        # Actions section
        body.append("Actions\n", style="bold")
        # Show last 15 actions to keep frame compact
        visible = _actions[-15:]
        for line in visible:
            body.append(f"  {line}\n")

        # Current streaming step
        if _current_stream["text"]:
            body.append(f"  ↳ {_current_stream['text']}\n", style="dim")

        # SGR section (only if we have data)
        if _sgr["conf_history"]:
            body.append("\n")
            # Separator
            body.append("─── SGR · ", style="dim")
            _cc = {"low": "red", "medium": "yellow", "high": "green"}
            for i, (_, conf) in enumerate(_sgr["conf_history"]):
                if i:
                    body.append(" → ", style="dim")
                body.append(conf, style=_cc.get(conf, "white"))
            body.append(" ─" * 20 + "\n", style="dim")

            # Open questions
            if _sgr["questions"]:
                body.append("\n  Open", style="bold")
                body.append(f" ({len(_sgr['questions'])})\n", style="dim")
                for q, age in _sgr["questions"].items():
                    if age == 0:
                        dot, color, tag = "●", "green", "  new"
                    elif age == 1:
                        dot, color, tag = "●", "yellow", ""
                    else:
                        dot, color, tag = "●", "red", f"  ⚠"
                    display_q = (q[:60] + "…") if len(q) > 62 else q
                    step_opened = _sgr["step_opened"].get(q, "?")
                    body.append(f"    {dot} ", style=f"bold {color}")
                    body.append(display_q, style="")
                    body.append(f"  step {step_opened}  age {age}{tag}\n", style="dim")

            # Resolved questions (last 5)
            if _sgr["resolutions"]:
                body.append(f"\n  Resolved ({len(_sgr['resolutions'])})\n", style="bold")
                for step_r, q, res_type, summary in _sgr["resolutions"][-5:]:
                    icon = "✓" if res_type == "answered" else "✗"
                    color = "green" if res_type == "answered" else "dim"
                    display_q = (q[:38] + "…") if len(q) > 40 else q
                    body.append(f"    {icon} ", style=f"bold {color}")
                    body.append(f"{display_q}", style="dim")
                    body.append(f"  step {step_r}\n", style="dim")
                    if summary:
                        display_s = (summary[:76] + "…") if len(summary) > 78 else summary
                        body.append(f"      {display_s}\n", style="dim italic")

        # Title with step and budget info
        step = _budget["step"]
        max_steps = _budget["max_steps"]
        tok_in = _budget["tok_in"]
        pct = int(100 * step / max_steps) if max_steps else 0
        last_conf = _sgr["conf_history"][-1][1] if _sgr["conf_history"] else ""
        conf_str = f" · conf={last_conf}" if last_conf else ""
        agent_name = _active["agent_name"]
        title = f"{agent_name} · step {step}/{max_steps} · {pct}% · ↑{tok_in}{conf_str}"

        return Panel(
            body,
            title=f"[dim]{title}[/dim]",
            border_style="dim blue",
            box=rich_box.ROUNDED,
            padding=(0, 1),
        )

    def _update_live() -> None:
        if live:
            live.update(_render_live_frame())

    # ── Render final SGR summary (logged permanently) ─────────────────────

    def _render_final_sgr() -> Panel:
        body = Text()

        # Confidence trajectory
        body.append("SGR · ", style="dim bold")
        _cc = {"low": "red", "medium": "yellow", "high": "green"}
        for i, (_, conf) in enumerate(_sgr["conf_history"]):
            if i:
                body.append(" → ", style="dim")
            body.append(conf, style=_cc.get(conf, "white"))
        body.append("\n")

        # Resolved
        answered = [(s, q, t, sm) for s, q, t, sm in _sgr["resolutions"] if t == "answered"]
        dropped = [(s, q, t, sm) for s, q, t, sm in _sgr["resolutions"] if t != "answered"]

        if answered:
            body.append(f"\nResolved ({len(answered)})\n", style="bold")
            for step_r, q, _, summary in answered:
                display_q = (q[:38] + "…") if len(q) > 40 else q
                body.append("  ✓ ", style="bold green")
                body.append(f"{display_q}", style="")
                if summary:
                    display_s = (summary[:70] + "…") if len(summary) > 72 else summary
                    body.append(f" → {display_s}", style="dim")
                body.append("\n")

        if dropped:
            body.append(f"\nDropped ({len(dropped)})\n", style="bold")
            for step_r, q, _, summary in dropped:
                display_q = (q[:38] + "…") if len(q) > 40 else q
                body.append("  ✗ ", style="bold dim")
                body.append(f"{display_q}", style="dim")
                if summary:
                    display_s = (summary[:70] + "…") if len(summary) > 72 else summary
                    body.append(f" → {display_s}", style="dim")
                body.append("\n")

        # Still open (shouldn't happen if agent finished properly)
        if _sgr["questions"]:
            body.append(f"\nStill open ({len(_sgr['questions'])})\n", style="bold yellow")
            for q in _sgr["questions"]:
                body.append(f"  ● {q}\n", style="yellow")

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
        in_str = f"↑{tok_in}[{tok_cached}]" if tok_cached else f"↑{tok_in}"
        return f"{in_str} ↓{tok_out}"

    # ── Event handler ─────────────────────────────────────────────────────

    def _switch_agent(agent_id: str, agent_name: str) -> None:
        """Switch live frame to a new agent. Log previous agent's final SGR if any."""
        prev = _active["agent_id"]
        if prev and prev != agent_id and _sgr["conf_history"]:
            # Print final SGR for the previous agent before switching
            (live.console if live else console).print(_render_final_sgr())
            # Reset SGR state for new agent
            _sgr["questions"].clear()
            _sgr["step_opened"].clear()
            _sgr["conf_history"].clear()
            _sgr["resolved_set"].clear()
            _sgr["resolutions"].clear()
            _actions.clear()
            _budget.update(step=0, tok_in=0, tok_out=0, tok_cached=0)

        _active["agent_id"] = agent_id
        _active["agent_name"] = agent_name

    def on_event(event: str, **kw) -> None:
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
            agent_id = kw.get("agent_id", "")
            agent_name = kw.get("agent_name", "agent")
            _switch_agent(agent_id, agent_name)
            _update_live()

        elif event == "orchestrator_plan_start":
            _log("[bold green]plan[/bold green]      strategist analyzing diff…")

        elif event == "orchestrator_agent_spawned":
            child = kw.get("child_id", "?")
            name = kw.get("agent_name", "?")
            focus = kw.get("focus", "")
            focus_short = (focus[:60] + "…") if len(focus) > 62 else focus
            if focus_short:
                _actions.append(f"[bold cyan]spawn {name}[/bold cyan] → {focus_short}")
            else:
                _actions.append(f"[bold cyan]spawn {name}[/bold cyan] ({child[:6]})")
            _update_live()

        elif event == "orchestrator_agent_done":
            # Will be followed by parent resuming — _switch_agent handles transition
            pass

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
            _update_live()

        elif event == "orchestrator_step":
            tool = kw.get("tool", "")
            args = kw.get("args", {})
            step = kw.get("step", 0)
            _budget["step"] = step + 1
            _budget["tok_in"] = kw.get("tok_in", _budget["tok_in"])
            _budget["tok_out"] = kw.get("tok_out", _budget["tok_out"])
            _budget["tok_cached"] = kw.get("tok_cached", _budget["tok_cached"])
            # Don't add to actions yet — wait for orchestrator_result

        elif event == "orchestrator_result":
            step = kw.get("step", 0)
            tool = kw.get("tool", "")
            result_count = kw.get("result_count")
            count_str = f"  ×{result_count}" if result_count is not None else ""
            tok_str = _fmt_tok_short()
            _actions.append(f"step {step}  {tool}{count_str}  {tok_str}")
            _current_stream["text"] = ""
            _update_live()

        elif event == "orchestrator_reflect":
            step = kw.get("step", 0)
            conf = kw.get("confidence", "?")
            questions = kw.get("questions_remaining", [])
            resolved_questions = kw.get("resolved_questions", [])

            # Update SGR state
            for rq in (resolved_questions or []):
                if not isinstance(rq, dict):
                    continue
                q_text = rq.get("question", "")
                res_type = rq.get("resolution", "answered")
                summary = rq.get("summary", "")
                if q_text and q_text not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(q_text)
                    _sgr["resolutions"].append((step, q_text, res_type, summary))

            new_q_set = set(questions)
            old_q_set = set(_sgr["questions"].keys())
            for q in (old_q_set - new_q_set):
                if q not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(q)
                    _sgr["resolutions"].append((step, q, "dropped", "(implicit)"))

            new_questions: dict = {}
            for q in questions:
                if q in _sgr["questions"]:
                    new_questions[q] = _sgr["questions"][q] + 1
                else:
                    new_questions[q] = 0
                    _sgr["step_opened"][q] = step
            _sgr["questions"] = new_questions
            _sgr["conf_history"].append((step, conf))

            # Add reflect to action log
            conf_color = {"low": "red", "medium": "yellow", "high": "green"}.get(conf, "white")
            _actions.append(f"step {step}  reflect()  conf=[{conf_color}]{conf}[/{conf_color}]")
            _current_stream["text"] = ""
            _update_live()

        elif event == "orchestrator_done":
            # Clear live frame
            if live:
                live.update("")
            # Print final SGR summary to log (permanent)
            if _sgr["conf_history"]:
                (live.console if live else console).print(_render_final_sgr())
            # Done message
            _log(
                f"[bold green]done[/bold green]      "
                f"[dim]{kw.get('findings', 0)} finding(s)  "
                f"{kw.get('replies', 0)} replies  "
                f"{kw.get('resolves', 0)} resolves[/dim]"
            )

        elif event == "orchestrator_forced_done":
            if live:
                live.update("")
            if _sgr["conf_history"]:
                (live.console if live else console).print(_render_final_sgr())
            _log(
                f"[yellow]forced[/yellow]    {kw.get('reason', 'limit')}  "
                f"[dim cyan]{_fmt_tok_short()}[/dim cyan]"
            )

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
