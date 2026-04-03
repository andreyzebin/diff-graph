#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

app = typer.Typer(help="DiffGraph — build a dependency metamodel from a git diff.", add_completion=False)
console = Console()

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"


# ── config ───────────────────────────────────────────────────────────────────

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


def _expand_env(s: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s)


def _expand_config(obj):
    if isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_config(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
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
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Path to the repository (after-version checkout)"),
    diff: Optional[str] = typer.Option(None, "--diff", "-d", help="Path to .diff file, or '-' for stdin"),
    pr_url: Optional[str] = typer.Option(None, "--pr-url", help="Bitbucket Server PR URL — clones repo and fetches diff automatically"),
    depth: Optional[int] = typer.Option(None, "--depth", help="BFS depth (default: from config, usually 2)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model override"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write rendered context to file instead of stdout"),
    dump_graph: Optional[str] = typer.Option(None, "--dump-graph", help="Write MetaModel as JSON array to file"),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="OpenAI-compatible API base URL override"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key override"),
    review: bool = typer.Option(False, "--review", help="Run review agent for curated context instead of BFS renderer"),
    post_comments: bool = typer.Option(False, "--post-comments", help="Post review comments to the PR (requires --pr-url and --review)"),
):
    """
    Build a dependency graph from a diff and render a prompt context.

    Examples:

    \b
      python cli.py run --repo ./my-service --diff changes.diff
      git diff HEAD~1 | python cli.py run --repo . --diff -
      python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42 --review
    """
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})
    render_cfg = cfg.get("render", {})
    explore_cfg = cfg.get("explore", {})

    if api_url:
        llm_cfg["api_url"] = api_url
    if api_key:
        llm_cfg["api_key"] = api_key
    if model:
        llm_cfg["model"] = model

    effective_depth = depth if depth is not None else explore_cfg.get("depth", 2)
    effective_model = llm_cfg.get("model", "gpt-4o-mini")
    max_tokens = render_cfg.get("max_tokens", 8000)
    max_callers = explore_cfg.get("max_callers", 5)
    exclude_tests = explore_cfg.get("exclude_tests", True)
    max_agent_steps = explore_cfg.get("max_agent_steps", 12)
    max_agent_tokens = explore_cfg.get("max_agent_tokens", 20000)

    cleanup_fn = None

    pr_title = ""
    pr_description = ""

    if pr_url:
        from diffgraph.providers.bitbucket_server import fetch_pr
        console.print(f"[bold]PR[/bold]  [cyan]{pr_url}[/cyan]")
        try:
            diff_text, _tmpdir, cleanup_fn, pr_meta = fetch_pr(
                pr_url,
                on_status=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            )
        except Exception as exc:
            console.print(f"[red]Failed to fetch PR: {exc}[/red]")
            raise typer.Exit(1)
        repo_path = _tmpdir
        pr_title = pr_meta.get("title", "")
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

    console.print(f"[bold]DiffGraph[/bold]  repo=[cyan]{repo_path}[/cyan]  depth=[cyan]{effective_depth}[/cyan]  model=[cyan]{effective_model}[/cyan]")

    llm_client = _make_llm_client(llm_cfg)

    sys.path.insert(0, str(BASE_DIR))
    from diffgraph import DiffGraph
    from diffgraph.diff_parser import parse_diff

    # Show a quick summary of what's in the diff
    diff_result = parse_diff(diff_text)
    _print_diff_summary(diff_result)

    dg = DiffGraph(
        repo_path=repo_path,
        llm_client=llm_client,
        llm_model=effective_model,
        max_tokens_in_prompt=max_tokens,
        max_callers=max_callers,
        exclude_tests=exclude_tests,
        max_agent_steps=max_agent_steps,
        max_agent_tokens=max_agent_tokens,
    )
    console.print("")
    with Live("", console=console, refresh_per_second=8, vertical_overflow="visible") as live:
        meta, diff_result = dg.build(
            diff_text,
            depth=effective_depth,
            on_event=_make_event_handler(effective_model, live),
        )
    console.print("")

    _print_model_summary(meta)

    if review:
        console.print("\n[bold]Review Agent[/bold]  curating context...\n")
        with Live("", console=console, refresh_per_second=8, vertical_overflow="visible") as live2:
            context = dg.review(meta, diff_result,
                                on_event=_make_event_handler(effective_model, live2),
                                pr_title=pr_title, pr_description=pr_description)
    else:
        context = dg.render(meta, diff_result, pr_title=pr_title, pr_description=pr_description)

    if post_comments and review and pr_url:
        from diffgraph.agents.reviewer import generate_review_comments
        from diffgraph.providers.bitbucket_server import post_review_comments
        console.print("\n[bold]Reviewer[/bold]  generating comments...\n")
        comments = generate_review_comments(
            context, llm_client, effective_model,
            on_event=_make_event_handler(effective_model, None),
        )
        console.print(f"  [dim]{len(comments)} comment(s) generated[/dim]")
        if comments:
            console.print("[bold]Posting[/bold]  to PR...\n")
            posted = post_review_comments(
                pr_url, comments,
                on_status=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            )
            console.print(f"\n[green]Posted {posted}/{len(comments)} comments[/green]")
    elif post_comments and not pr_url:
        console.print("[yellow]--post-comments requires --pr-url[/yellow]")

    if dump_graph:
        import json
        Path(dump_graph).write_text(json.dumps(meta.to_json(), ensure_ascii=False, indent=2))
        console.print(f"[green]Graph written to {dump_graph}[/green]  ({len(meta.modules)} modules)")

    if output:
        Path(output).write_text(context)
        console.print(f"\n[green]Context written to {output}[/green]  ({len(context)} chars, ~{len(context)//4} tokens)")
    else:
        console.print("\n" + "─" * 70)
        console.print(context, markup=False, highlight=False)
        console.print("─" * 70)
        console.print(f"[dim]{len(context)} chars, ~{len(context)//4} tokens[/dim]")

    if cleanup_fn:
        cleanup_fn()


@app.command()
def inspect(
    diff: Optional[str] = typer.Argument(None, help="Path to .diff file, or '-' for stdin"),
):
    """
    Parse a diff and show what changed — no LLM required.

    Useful for verifying the diff parser before running the full pipeline.

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


# ── formatting helpers ────────────────────────────────────────────────────────

def _print_diff_summary(diff_result, verbose: bool = False) -> None:
    from diffgraph.diff_parser import DiffResult

    table = Table(title="Diff summary", show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Status")
    table.add_column("Changed lines", justify="right")
    table.add_column("Hunks", justify="right")

    for fd in diff_result.files.values():
        status_color = {
            "modified": "yellow",
            "added": "green",
            "deleted": "red",
            "renamed": "cyan",
        }.get(fd.status, "white")
        table.add_row(
            fd.path,
            f"[{status_color}]{fd.status}[/{status_color}]",
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


def _make_event_handler(model: str, live: Optional[Live]):
    """Returns an on_event callback that logs progress via a Rich Live display."""
    depth_colors = ["bold cyan", "cyan", "dim cyan"]
    state: dict = {"path": "", "tok": 0}

    def _log(msg: str) -> None:
        (live.console if live else console).log(msg)

    def _live_update(text: str) -> None:
        if live:
            live.update(text)

    def on_event(event: str, **kw) -> None:
        depth = kw.get("depth", 0)
        path = kw.get("path", "")
        name = kw.get("name", "")
        color = depth_colors[min(depth, len(depth_colors) - 1)]

        if event == "reading":
            state["path"] = path
            _log(f"[{color}]read[/{color}]      [bold]{path}[/bold]  [dim]depth={depth}[/dim]")

        elif event == "extracting":
            state["path"] = path
            state["tok"] = 0
            attempt = kw.get("attempt", 0)
            suffix = f"  [yellow]retry {attempt}[/yellow]" if attempt else f"  [dim]{model}[/dim]"
            _log(f"[{color}]extract[/{color}]   [bold]{path}[/bold]{suffix}")

        elif event == "token":
            text = kw.get("text", "")
            state["tok"] += 1
            # Collapse whitespace and show a rolling tail of the stream
            preview = " ".join(text.split())[-100:]
            _live_update(
                Text.assemble(
                    ("  ↳ ", "dim"),
                    (f"{state['path']}  ", "bold"),
                    (f"{state['tok']} tok  ", "dim"),
                    (preview, "dim cyan"),
                )
            )

        elif event == "cache_hit":
            _log(
                f"[dim green]cached[/dim green]    [bold]{path}[/bold]"
                f"  [dim]{kw.get('symbols', 0)} symbols[/dim]"
            )

        elif event == "extracted":
            _live_update("")  # clear the streaming line
            deps = kw.get("deps", [])
            dep_names = [d.name if hasattr(d, "name") else str(d) for d in deps]
            dep_str = ", ".join(dep_names[:6]) + ("…" if len(dep_names) > 6 else "")
            _log(
                f"[green]done[/green]      [bold]{path}[/bold]"
                f"  [dim]{kw.get('symbols', 0)} symbols"
                + (f"  deps: [{dep_str}]" if deps else "") + "[/dim]"
            )

        elif event == "retry":
            _live_update("")
            _log(
                f"[yellow]retry {kw.get('attempt')}[/yellow]   [bold]{path}[/bold]"
                f"  [dim]{kw.get('reason', '')}[/dim]"
            )

        elif event == "failed":
            _live_update("")
            _log(f"[red]failed[/red]    [bold]{path}[/bold]  [dim]skipped after 3 attempts[/dim]")

        elif event == "read_failed":
            _log(f"[red]missing[/red]   [bold]{path}[/bold]  [dim]file not found[/dim]")

        elif event == "skipped":
            _log(f"[dim]  skip      {path}  not a source file[/dim]")

        elif event == "resolving":
            pass  # too noisy — only log when resolved or skipped

        elif event == "resolving_agent":
            _live_update("")
            _log(f"[cyan]resolve[/cyan]   agent resolving [bold]{name}[/bold]  [dim]{kw.get('fqn', '')}[/dim]")

        elif event == "resolved":
            _live_update("")
            tok_str = _fmt_tok(kw)
            suffix = f"  [dim cyan]{tok_str}[/dim cyan]" if tok_str else ""
            _log(f"[dim]  resolve   {name}  →  {kw.get('path', '')}[/dim]{suffix}")

        elif event == "not_resolved":
            _live_update("")
            tok_str = _fmt_tok(kw)
            suffix = f"  [dim cyan]{tok_str}[/dim cyan]" if tok_str else ""
            _log(f"[dim]  resolve   {name}  →  external, skip[/dim]{suffix}")

        elif event == "searching_callers":
            _log(f"[magenta]impact[/magenta]    agent analyzing impact of [bold]{name}[/bold]")

        elif event == "agent_step":
            tool = kw.get("tool", "")
            args = kw.get("args", {})
            arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            tok_suffix = _fmt_tok(kw)
            _live_update(
                Text.assemble(
                    ("  ↳ ", "dim"),
                    (f"step {kw.get('step', 0)}  ", "dim"),
                    (tool, "magenta"),
                    (f"({arg_str})", "dim"),
                    (tok_suffix, "dim cyan"),
                )
            )

        elif event == "agent_result":
            pass  # live line already shows current step

        elif event == "agent_done":
            _live_update("")
            hits = kw.get("hits", 0)
            tok_str = _fmt_tok(kw)
            _log(f"[magenta]impact[/magenta]    [bold]{path}[/bold]  [dim]{hits} impacted file(s) found[/dim]  [dim cyan]{tok_str}[/dim cyan]")

        elif event == "agent_forced_done":
            _live_update("")
            reason = kw.get("reason", "limit reached")
            tok_str = _fmt_tok(kw)
            _log(f"[yellow]impact[/yellow]    [bold]{path}[/bold]  [dim]{reason}[/dim]  [dim cyan]{tok_str}[/dim cyan]")

        elif event == "reviewer_start":
            _log("[bold cyan]reviewer[/bold cyan]  calling LLM…")

        elif event == "reviewer_done":
            tok_str = _fmt_tok(kw)
            _log(f"[bold cyan]reviewer[/bold cyan]  done  [dim cyan]{tok_str}[/dim cyan]")

        elif event == "reviewer_failed":
            _log(f"[red]reviewer[/red]  failed: {kw.get('reason', '')}")

        elif event == "review_start":
            _log(f"[bold cyan]review[/bold cyan]    agent starting  [dim]{kw.get('changed', 0)} changed module(s)[/dim]")

        elif event == "review_step":
            tool = kw.get("tool", "")
            args = kw.get("args", {})
            arg_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            tok_suffix = _fmt_tok(kw)
            _log(f"[dim]  review  step {kw.get('step', 0)}  [cyan]{tool}[/cyan]({arg_str})  {tok_suffix}[/dim]")

        elif event == "review_result":
            pass  # step line already logged

        elif event == "review_selected":
            detail = kw.get("detail", "")
            _log(
                f"[green]select[/green]    [bold]{kw.get('symbol_name', kw.get('name', ''))}[/bold]"
                f"  [dim]{kw.get('file', '')}[/dim]"
                f"  [yellow]{detail}[/yellow]"
                f"  [dim]{kw.get('reason', '')[:80]}[/dim]"
            )

        elif event == "review_done":
            _live_update("")
            tok_str = _fmt_tok(kw)
            _log(f"[bold cyan]review[/bold cyan]    done  [dim]{kw.get('count', 0)} symbols selected[/dim]  [dim cyan]{tok_str}[/dim cyan]")

        elif event == "review_forced_done":
            _live_update("")
            tok_str = _fmt_tok(kw)
            _log(f"[yellow]review[/yellow]    {kw.get('reason', 'limit')}  [dim cyan]{tok_str}[/dim cyan]")

        elif event == "caller_found":
            conf = kw.get("confidence", "")
            reason = kw.get("reason", "")
            _log(
                f"[magenta]caller[/magenta]    [bold]{path}[/bold]"
                f"  [{conf}]  [dim]{reason[:80]}[/dim]"
            )

    return on_event


def _fmt_tok(kw: dict) -> str:
    """Format token breakdown from an agent event's keyword args."""
    tok_in = kw.get("tok_in", 0)
    tok_out = kw.get("tok_out", 0)
    tok_cached = kw.get("tok_cached", 0)
    if not (tok_in or tok_out):
        total = kw.get("total_tokens", 0)
        return f"{total} tok" if total else ""
    parts = [f"in={tok_in}", f"out={tok_out}"]
    if tok_cached:
        parts.append(f"cached={tok_cached}")
    return "  ".join(parts)


def _print_model_summary(meta) -> None:
    changed = len(meta.changed_module_ids)
    total = len(meta.modules)
    sym_changed = sum(1 for mod in meta.modules.values() for s in mod.symbols if s.is_changed)

    depths = meta.compute_depths()
    by_depth: dict[int, int] = {}
    for d in depths.values():
        by_depth[d] = by_depth.get(d, 0) + 1

    depth_str = "  ".join(
        (f"callers: {n}" if d == -1 else f"depth {d}: {n}")
        for d, n in sorted(by_depth.items())
    )
    console.print(
        f"[bold]MetaModel[/bold]  {total} modules ({changed} changed, {sym_changed} changed symbols)  {depth_str}"
    )


if __name__ == "__main__":
    app()
