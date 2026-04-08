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
    # SGR convergence state — updated on every reflect event
    _sgr: dict = {
        "questions":     {},   # question_text -> age (number of reflects it has been open)
        "step_opened":   {},   # question_text -> step number when first seen
        "conf_history":  [],   # [(step, conf), ...]
        "resolved_set":  set(),# unique question texts that have been resolved/dropped
        "resolutions":   [],   # [(step, question, resolution_type, summary), ...]
    }

    def _render_convergence() -> Panel:
        body = Text()

        # Confidence trajectory
        if _sgr["conf_history"]:
            _cc = {"low": "red", "medium": "yellow", "high": "green"}
            body.append("conf: ", style="dim")
            for i, (_, conf) in enumerate(_sgr["conf_history"]):
                if i:
                    body.append(" → ", style="dim")
                body.append(conf, style=_cc.get(conf, "white"))
            body.append("\n\n")

        # Open questions with age-based colouring
        if _sgr["questions"]:
            for q, age in _sgr["questions"].items():
                if age == 0:
                    dot, color, tag = "●", "green", " new"
                elif age == 1:
                    dot, color, tag = "●", "yellow", ""
                else:
                    dot, color, tag = "●", "red", f" [!×{age}]"
                display_q = (q[:58] + "…") if len(q) > 60 else q
                step_opened = _sgr["step_opened"].get(q, "?")
                body.append(f"  {dot} ", style=f"bold {color}")
                body.append(display_q)
                body.append(f"  step {step_opened}  age {age}{tag}\n", style="dim")
        else:
            body.append("  (no open questions)\n", style="dim")

        if _sgr["resolutions"]:
            body.append("\nResolved\n", style="bold")
            for step_r, q, res_type, summary in _sgr["resolutions"][-5:]:  # last 5
                icon  = "✓" if res_type == "answered" else "✗"
                color = "green" if res_type == "answered" else "dim"
                display_q = (q[:40] + "…") if len(q) > 42 else q
                body.append(f"  {icon} ", style=f"bold {color}")
                body.append(f"{display_q}", style="dim")
                body.append(f"  step {step_r}\n", style="dim")
                if summary:
                    body.append(f"    {summary[:80]}\n", style="dim italic")

        n_open = len(_sgr["questions"])
        last_conf = _sgr["conf_history"][-1][1] if _sgr["conf_history"] else "?"
        last_conf_color = {"low": "red", "medium": "yellow", "high": "green"}.get(last_conf, "white")
        return Panel(
            body,
            title=(
                f"[dim]SGR  {n_open} open  "
                f"conf=[{last_conf_color}]{last_conf}[/{last_conf_color}][/dim]"
            ),
            border_style="dim blue",
            box=rich_box.ROUNDED,
            padding=(0, 1),
        )

    def _log(msg: str) -> None:
        (live.console if live else console).log(msg)

    def _live_update(text) -> None:
        if live:
            live.update(text)

    def _restore_convergence() -> None:
        """Restore convergence panel after transient live updates (streaming, etc.)."""
        if _sgr["conf_history"]:
            _live_update(_render_convergence())
        else:
            _live_update("")

    def on_event(event: str, **kw) -> None:
        if event == "orchestrator_agent_compiled":
            name = kw.get("name", "?")
            mode = kw.get("mode", "?")
            caps = kw.get("capabilities", "–")
            data = kw.get("data", "–")
            bt = kw.get("budget_tokens", 0)
            bs = kw.get("budget_steps", 0)
            _log(f"[dim]  compiled [cyan]{name}[/cyan] [{mode}]  caps=[{caps}]  data=[{data}]  budget={bt}t/{bs}s[/dim]")

        elif event == "orchestrator_plan_start":
            _log("[bold green]plan[/bold green]      strategist analyzing diff…")

        elif event == "orchestrator_plan_done":
            plan        = kw.get("plan", {})
            system_type = plan.get("system_type", "?")
            tasks       = plan.get("tasks", [])

            pri_color = {"high": "red", "medium": "yellow", "low": "green"}

            body = Text()
            for t in tasks:
                pri   = t.get("priority", "?")
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
                border_style="dim",
                box=rich_box.ROUNDED,
                padding=(0, 1),
            )
            (live.console if live else console).print(panel)

        elif event == "orchestrator_stream":
            tool_name    = kw.get("tool_name", "")
            args_preview = kw.get("args_preview", "")
            tok          = kw.get("tok", 0)
            _live_update(Text.assemble(
                ("  ↳ ", "dim"),
                (f"step {kw.get('step', 0)}  ", "dim"),
                (tool_name or "…", "green"),
                (f"({args_preview})", "dim"),
                (f"  {tok} tok", "dim cyan"),
            ))

        elif event == "orchestrator_step":
            tool    = kw.get("tool", "")
            args    = kw.get("args", {})
            arg_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            tok_str = _fmt_tok(kw)
            _log(f"[dim]  step {kw.get('step', 0)}  [green]{tool}[/green]({arg_str})  {tok_str}[/dim]")
            _restore_convergence()

        elif event == "orchestrator_reflect":
            step               = kw.get("step", 0)
            conf               = kw.get("confidence", "?")
            learned            = str(kw.get("learned", ""))
            questions          = kw.get("questions_remaining", [])
            resolved_questions = kw.get("resolved_questions", [])
            next_action        = str(kw.get("next_action", ""))

            # ── Update SGR convergence state ──────────────────────────────────
            # Record explicit resolutions from the LLM
            for rq in (resolved_questions or []):
                if not isinstance(rq, dict):
                    continue
                q_text   = rq.get("question", "")
                res_type = rq.get("resolution", "answered")
                summary  = rq.get("summary", "")
                if q_text and q_text not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(q_text)
                    _sgr["resolutions"].append((step, q_text, res_type, summary))

            # Also silently-resolved questions (disappeared without explicit entry)
            new_q_set = set(questions)
            old_q_set = set(_sgr["questions"].keys())
            for q in (old_q_set - new_q_set):
                if q not in _sgr["resolved_set"]:
                    _sgr["resolved_set"].add(q)
                    _sgr["resolutions"].append((step, q, "dropped", "(implicit)"))

            new_questions: dict = {}
            for q in questions:
                if q in _sgr["questions"]:
                    new_questions[q] = _sgr["questions"][q] + 1  # existing: increment age
                else:
                    new_questions[q] = 0                          # new or reappearing: age 0
                    _sgr["step_opened"][q] = step                 # always sync with age reset
            _sgr["questions"] = new_questions
            _sgr["conf_history"].append((step, conf))

            # ── Print reflect panel (scrolls into history) ────────────────────
            conf_color = {"low": "red", "medium": "yellow", "high": "green"}.get(conf, "white")
            body = Text()
            if learned:
                body.append("Learned\n", style="bold")
                for line in learned.splitlines():
                    body.append(f"  {line}\n", style="")
            if resolved_questions:
                body.append("\nResolved\n", style="bold")
                for rq in resolved_questions:
                    if not isinstance(rq, dict):
                        continue
                    icon  = "✓" if rq.get("resolution") == "answered" else "✗"
                    color = "green" if rq.get("resolution") == "answered" else "dim"
                    body.append(f"  {icon} ", style=f"bold {color}")
                    body.append(f"{rq.get('question', '')}\n", style="dim")
                    if rq.get("summary"):
                        body.append(f"    {rq['summary']}\n", style="dim italic")
            if questions:
                body.append("\nOpen\n", style="bold")
                for q in questions:
                    age = new_questions.get(q, 0)
                    age_color = ("green" if age == 0 else "yellow" if age == 1 else "red")
                    body.append("  • ", style=f"bold {age_color}")
                    body.append(f"{q}\n", style="dim")
            if next_action:
                body.append("\nNext\n", style="bold green")
                body.append(f"  {next_action}", style="")

            panel = Panel(
                body,
                title=f"[bold]reflect[/bold] · step {step} · conf=[{conf_color}]{conf}[/{conf_color}]",
                border_style="dim",
                box=rich_box.ROUNDED,
                padding=(0, 1),
            )
            (live.console if live else console).print(panel)

            # ── Update live convergence panel ─────────────────────────────────
            _live_update(_render_convergence())

        elif event == "orchestrator_result":
            pass

        elif event == "orchestrator_done":
            _live_update("")   # clear convergence panel before final log
            tok_str = _fmt_tok(kw)
            _log(
                f"[bold green]done[/bold green]      "
                f"[dim]{kw.get('findings', 0)} finding(s)  "
                f"{kw.get('replies', 0)} replies  "
                f"{kw.get('resolves', 0)} resolves[/dim]  "
                f"[dim cyan]{tok_str}[/dim cyan]"
            )

        elif event == "orchestrator_forced_done":
            _live_update("")   # clear convergence panel
            _log(
                f"[yellow]forced[/yellow]    {kw.get('reason', 'limit')}  "
                f"[dim cyan]{_fmt_tok(kw)}[/dim cyan]"
            )

    return on_event


def _fmt_tok(kw: dict) -> str:
    tok_in     = kw.get("tok_in", 0)
    tok_out    = kw.get("tok_out", 0)
    tok_cached = kw.get("tok_cached", 0)
    if not (tok_in or tok_out):
        return ""
    parts = [f"in={tok_in}", f"out={tok_out}"]
    if tok_cached:
        parts.append(f"cached={tok_cached}")
    return "  ".join(parts)


if __name__ == "__main__":
    app()
