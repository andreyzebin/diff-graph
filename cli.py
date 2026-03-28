from __future__ import annotations

import asyncio
import datetime
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Code Review Agent Benchmark", add_completion=False)
console = Console()

BASE_DIR = Path(__file__).parent
SCENARIOS_DIR = BASE_DIR / "scenarios"
RESULTS_DIR = BASE_DIR / "results"
CONFIG_FILE = BASE_DIR / "config.yaml"

# Make benchmark package importable
sys.path.insert(0, str(BASE_DIR))


def _load_config() -> dict:
    import yaml
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def _expand_env(s: str) -> str:
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s)


@app.command()
def run(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Run specific scenario by ID"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Filter by tag"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Load scenarios without calling agent"),
    compare_with: Optional[str] = typer.Option(None, "--compare-with", help="Compare with run ID or 'last'"),
    agent_url: Optional[str] = typer.Option(None, "--agent-url", help="Override agent URL from config"),
):
    """Run benchmark scenarios."""
    asyncio.run(_run_async(scenario, tag or [], dry_run, compare_with, agent_url))


async def _run_async(
    scenario_id: str | None,
    tags: list[str],
    dry_run: bool,
    compare_with: str | None,
    agent_url_override: str | None,
):
    from bitbucket import build_proxy
    from runner.scenario_loader import load_scenarios
    from runner.agent_client import AgentClient
    from runner.judge import LLMJudge, AnthropicLLMClient
    from runner.results_store import ResultsStore
    from runner.run import run_scenario

    cfg = _load_config()
    agent_cfg = cfg.get("agent", {})
    agent_url = agent_url_override or _expand_env(agent_cfg.get("base_url", "http://localhost:8080"))
    api_key = _expand_env(agent_cfg.get("api_key", ""))

    judge_cfg = cfg.get("judge", {})
    results_cfg = cfg.get("results", {})

    scenarios = load_scenarios(SCENARIOS_DIR, tags=tags, scenario_id=scenario_id)

    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        raise typer.Exit(1)

    if dry_run:
        console.print(f"\n[bold]Dry run — {len(scenarios)} scenario(s) found:[/bold]\n")
        for s in scenarios:
            req = len(s.expected_output.required_comments)
            console.print(
                f"  [cyan]{s.id:12}[/cyan] {s.name}  "
                f"[dim]{', '.join(s.tags)}  required={req}[/dim]"
            )
        raise typer.Exit(0)

    agent_client = AgentClient(
        base_url=agent_url,
        api_key=api_key,
        timeout=agent_cfg.get("timeout_seconds", 120),
    )
    judge = LLMJudge(AnthropicLLMClient(
        model=judge_cfg.get("model", "claude-opus-4-6"),
        temperature=judge_cfg.get("temperature", 0),
    ))
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    console.print(f"\n[bold]Running {len(scenarios)} scenario(s)...[/bold]\n")

    results = []
    for s in scenarios:
        proxy = await build_proxy(s.input["bitbucket"])
        async with proxy:
            result = await run_scenario(
                scenario=s,
                proxy=proxy,
                agent_client=agent_client,
                judge=judge,
            )
        results.append(result)

        if result.verdict == "pass":
            icon = "[green]✅[/green]"
        elif result.verdict == "error":
            icon = "[red]❌[/red]"
        else:
            icon = "[yellow]⚠️ [/yellow]"

        fail_reason = ""
        if result.verdict == "fail":
            fail_reason = f"  [red][FAIL: min_score={s.expected_output.thresholds.min_score:.2f}][/red]"

        console.print(
            f"{icon} {s.id:12} {s.name[:38]:38} "
            f"score=[bold]{result.score:.2f}[/bold]  "
            f"comments={result.total_comments}  "
            f"{result.duration_seconds:.1f}s"
            f"{fail_reason}"
        )

    passed = sum(1 for r in results if r.passed)
    avg_score = sum(r.score for r in results) / len(results)
    total_time = sum(r.duration_seconds for r in results)

    console.print(f"\n{'─' * 70}")
    console.print(
        f"[bold]Results : {passed}/{len(results)} passed   "
        f"avg_score={avg_score:.2f}   total={total_time:.1f}s[/bold]"
    )

    run_id = f"run-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    json_path = store.save_run(run_id, results, agent_url=agent_url, tags=tags)
    console.print(f"Saved   : {json_path}")

    if compare_with:
        prev_results = None
        if compare_with == "last":
            all_runs = store.list_runs(10)
            # Find the run before current
            for r in all_runs:
                if r["run_id"] != run_id:
                    prev_results = store.get_run_by_id(r["run_id"])
                    break
        else:
            prev_results = store.get_run_by_id(compare_with)

        if prev_results:
            _print_regression_table(results, prev_results)
        else:
            console.print("[yellow]No previous run found for comparison.[/yellow]")


def _print_regression_table(current: list, previous: list):
    prev_by_id = {r.scenario_id: r for r in previous}
    table = Table(title="Regression comparison")
    table.add_column("Scenario")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Status")

    for r in current:
        prev = prev_by_id.get(r.scenario_id)
        if prev:
            delta = r.score - prev.score
            if delta > 0.01:
                status = "✅ улучшение"
            elif delta < -0.01:
                status = "⚠️  регресс"
            else:
                status = "➡️  без изменений"
            table.add_row(
                r.scenario_id,
                f"{prev.score:.2f}",
                f"{r.score:.2f}",
                f"{delta:+.2f}",
                status,
            )
    console.print(table)


@app.command()
def report(
    run_id: str = typer.Argument("last", help="Run ID or 'last'"),
):
    """Show report for a run."""
    from runner.results_store import ResultsStore

    cfg = _load_config()
    results_cfg = cfg.get("results", {})
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    if run_id == "last":
        results = store.get_last_run()
    else:
        results = store.get_run_by_id(run_id)

    if not results:
        console.print("[red]Run not found.[/red]")
        raise typer.Exit(1)

    table = Table(title=f"Run: {run_id}")
    table.add_column("Scenario")
    table.add_column("Verdict")
    table.add_column("Score", justify="right")
    table.add_column("Required", justify="right")
    table.add_column("FP", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Summary")

    for r in results:
        icon = "✅" if r.passed else "❌"
        table.add_row(
            r.scenario_id,
            f"{icon} {r.verdict}",
            f"{r.score:.2f}",
            f"{r.required_found}/{r.required_total}",
            str(r.false_positives),
            f"{r.duration_seconds:.1f}s",
            r.judge_summary[:50],
        )
    console.print(table)


@app.command()
def history(limit: int = typer.Option(20, "--limit", "-n", help="Number of runs to show")):
    """Show run history."""
    from runner.results_store import ResultsStore

    cfg = _load_config()
    results_cfg = cfg.get("results", {})
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    runs = store.list_runs(limit)
    if not runs:
        console.print("[yellow]No runs found.[/yellow]")
        return

    table = Table(title="Run history")
    table.add_column("Run ID")
    table.add_column("Date")
    table.add_column("Passed", justify="right")
    table.add_column("Avg Score", justify="right")
    for r in runs:
        table.add_row(
            r["run_id"],
            r["run_at"][:19],
            f"{r['passed']}/{r['total']}",
            f"{r['avg_score']:.3f}",
        )
    console.print(table)


@app.command()
def ab(
    agent_a: str = typer.Option(..., "--agent-a", help="URL of agent A"),
    agent_b: str = typer.Option(..., "--agent-b", help="URL of agent B"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Filter by tag"),
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Specific scenario ID"),
):
    """A/B comparison of two agent versions."""
    asyncio.run(_ab_async(agent_a, agent_b, tag or [], scenario))


async def _ab_async(agent_a: str, agent_b: str, tags: list[str], scenario_id: str | None):
    from bitbucket import build_proxy
    from runner.scenario_loader import load_scenarios
    from runner.agent_client import AgentClient
    from runner.judge import LLMJudge, AnthropicLLMClient
    from runner.run import run_scenario

    cfg = _load_config()
    judge_cfg = cfg.get("judge", {})
    agent_cfg = cfg.get("agent", {})
    api_key = _expand_env(agent_cfg.get("api_key", ""))

    scenarios = load_scenarios(SCENARIOS_DIR, tags=tags, scenario_id=scenario_id)
    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        raise typer.Exit(1)

    client_a = AgentClient(agent_a, api_key)
    client_b = AgentClient(agent_b, api_key)
    judge = LLMJudge(AnthropicLLMClient(model=judge_cfg.get("model", "claude-opus-4-6")))

    console.print(f"\n[bold]A/B test: {len(scenarios)} scenario(s)[/bold]")
    console.print(f"  Agent A: {agent_a}")
    console.print(f"  Agent B: {agent_b}\n")

    results_a, results_b = [], []
    for s in scenarios:
        console.print(f"Running {s.id}...")
        proxy_a = await build_proxy(s.input["bitbucket"])
        async with proxy_a:
            ra = await run_scenario(s, proxy_a, client_a, judge)
        proxy_b = await build_proxy(s.input["bitbucket"])
        async with proxy_b:
            rb = await run_scenario(s, proxy_b, client_b, judge)
        results_a.append(ra)
        results_b.append(rb)

    table = Table(title="A/B Comparison")
    table.add_column("Scenario")
    table.add_column("Agent A", justify="right")
    table.add_column("Agent B", justify="right")
    table.add_column("Winner")

    total_a = total_b = 0.0
    for ra, rb, s in zip(results_a, results_b, scenarios):
        total_a += ra.score
        total_b += rb.score
        if ra.score > rb.score + 0.01:
            winner = "[green]A[/green]"
        elif rb.score > ra.score + 0.01:
            winner = "[blue]B[/blue]"
        else:
            winner = "tie"
        table.add_row(s.id, f"{ra.score:.2f}", f"{rb.score:.2f}", winner)

    n = len(scenarios)
    avg_a = total_a / n
    avg_b = total_b / n
    delta = avg_b - avg_a
    if avg_a > avg_b + 0.005:
        overall = f"[green]A[/green] ({delta:+.2f})"
    elif avg_b > avg_a + 0.005:
        overall = f"[blue]B[/blue] ({delta:+.2f})"
    else:
        overall = "tie"

    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{avg_a:.2f}[/bold]",
        f"[bold]{avg_b:.2f}[/bold]",
        f"[bold]{overall}[/bold]",
    )
    console.print(table)


if __name__ == "__main__":
    app()
