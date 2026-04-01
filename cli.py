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
    repo: str = typer.Option(..., "--repo", "-r", help="Path to the repository (after-version checkout)"),
    diff: Optional[str] = typer.Option(None, "--diff", "-d", help="Path to .diff file, or '-' for stdin"),
    depth: Optional[int] = typer.Option(None, "--depth", help="BFS depth (default: from config, usually 2)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model override"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write rendered context to file instead of stdout"),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="OpenAI-compatible API base URL override"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key override"),
):
    """
    Build a dependency graph from a diff and render a prompt context.

    Examples:

    \b
      python cli.py run --repo ./my-service --diff changes.diff
      git diff HEAD~1 | python cli.py run --repo . --diff -
      python cli.py run --repo . --diff my.diff --depth 1 --output context.txt
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

    diff_text = _read_diff(diff)
    if not diff_text.strip():
        console.print("[yellow]Diff is empty — nothing to do.[/yellow]")
        raise typer.Exit(0)

    repo_path = str(Path(repo).resolve())

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

    context = dg.render(meta, diff_result)

    if output:
        Path(output).write_text(context)
        console.print(f"\n[green]Context written to {output}[/green]  ({len(context)} chars, ~{len(context)//4} tokens)")
    else:
        console.print("\n" + "─" * 70)
        console.print(context, markup=False, highlight=False)
        console.print("─" * 70)
        console.print(f"[dim]{len(context)} chars, ~{len(context)//4} tokens[/dim]")


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


def _make_event_handler(model: str, live: Live):
    """Returns an on_event callback that logs progress via a Rich Live display."""
    depth_colors = ["bold cyan", "cyan", "dim cyan"]
    state: dict = {"path": "", "tok": 0}

    def _log(msg: str) -> None:
        live.console.log(msg)

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
            live.update(
                Text.assemble(
                    ("  ↳ ", "dim"),
                    (f"{state['path']}  ", "bold"),
                    (f"{state['tok']} tok  ", "dim"),
                    (preview, "dim cyan"),
                )
            )

        elif event == "extracted":
            live.update("")  # clear the streaming line
            deps = kw.get("deps", [])
            dep_str = ", ".join(deps[:6]) + ("…" if len(deps) > 6 else "")
            _log(
                f"[green]done[/green]      [bold]{path}[/bold]"
                f"  [dim]{kw.get('symbols', 0)} symbols"
                + (f"  deps: [{dep_str}]" if deps else "") + "[/dim]"
            )

        elif event == "retry":
            live.update("")
            _log(
                f"[yellow]retry {kw.get('attempt')}[/yellow]   [bold]{path}[/bold]"
                f"  [dim]{kw.get('reason', '')}[/dim]"
            )

        elif event == "failed":
            live.update("")
            _log(f"[red]failed[/red]    [bold]{path}[/bold]  [dim]skipped after 3 attempts[/dim]")

        elif event == "read_failed":
            _log(f"[red]missing[/red]   [bold]{path}[/bold]  [dim]file not found[/dim]")

        elif event == "resolving":
            pass  # too noisy — only log when resolved or skipped

        elif event == "resolved":
            _log(f"[dim]  resolve   {name}  →  {kw.get('path', '')}[/dim]")

        elif event == "not_resolved":
            _log(f"[dim]  resolve   {name}  →  external, skip[/dim]")

    return on_event


def _print_model_summary(meta) -> None:
    changed = len(meta.changed_module_ids)
    total = len(meta.modules)
    sym_changed = len(meta.changed_symbol_names)

    by_depth: dict[int, int] = {}
    for mod in meta.modules.values():
        by_depth[mod.depth] = by_depth.get(mod.depth, 0) + 1

    depth_str = "  ".join(f"depth {d}: {n}" for d, n in sorted(by_depth.items()))
    console.print(
        f"[bold]MetaModel[/bold]  {total} modules ({changed} changed, {sym_changed} changed symbols)  {depth_str}"
    )


if __name__ == "__main__":
    app()
