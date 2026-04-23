from __future__ import annotations

import os
# Force UTF-8 mode (fixes Windows cp1251 issues with redirect to file)
os.environ.setdefault("PYTHONUTF8", "1")

# Use OS trust store for SSL (picks up corporate proxy CAs like CheckPoint, Zscaler)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import datetime
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


def _make_trigger(agent_cfg: dict, bitbucket_connection: dict):
    from runner.trigger import HttpTrigger, WebhookTrigger, CliTrigger
    mode = agent_cfg.get("trigger", "http")
    timeout = agent_cfg.get("timeout_seconds", 120)
    if mode == "webhook":
        agent_account = _expand_env(bitbucket_connection.get("agent_account", ""))
        return WebhookTrigger(agent_account=agent_account, timeout_seconds=timeout)
    if mode == "cli":
        command = agent_cfg.get("command", "")
        cwd = agent_cfg.get("cwd") or None
        output = agent_cfg.get("output", "log")
        base_url = bitbucket_connection.get("base_url", "").rstrip("/")
        project = bitbucket_connection.get("project", "")
        repo = bitbucket_connection.get("repo", "")
        pr_url_template = (
            f"{base_url}/projects/{project}/repos/{repo}/pull-requests/{{pr_id}}"
        )
        return CliTrigger(
            command_template=command,
            pr_url_template=pr_url_template,
            timeout_seconds=timeout,
            cwd=cwd,
            output=output,
        )
    # default: http
    from runner.agent_client import AgentClient
    base_url = _expand_env(agent_cfg.get("base_url", "http://localhost:8080"))
    api_key = _expand_env(agent_cfg.get("api_key", ""))
    return HttpTrigger(AgentClient(base_url=base_url, api_key=api_key, timeout=timeout))


def _print_trigger_summary(agent_cfg: dict, console) -> None:
    mode = agent_cfg.get("trigger", "http")
    timeout = agent_cfg.get("timeout_seconds", 120)
    if mode == "cli":
        cmd = agent_cfg.get("command", "")
        cwd = agent_cfg.get("cwd", "")
        output = agent_cfg.get("output", "log")
        console.print(f"Trigger : [cyan]cli[/cyan]  timeout={timeout}s  cwd={cwd or '(current)'}  output={output}")
        console.print(f"Command : [dim]{cmd}[/dim]")
    elif mode == "webhook":
        console.print(f"Trigger : [cyan]webhook[/cyan]  timeout={timeout}s")
    else:
        base_url = agent_cfg.get("base_url", "http://localhost:8080")
        console.print(f"Trigger : [cyan]http[/cyan]  url={base_url}  timeout={timeout}s")


def _make_llm_client(judge_cfg: dict):
    from runner.judge import AnthropicLLMClient, OpenAILLMClient
    model = judge_cfg.get("model", "claude-opus-4-6")
    temperature = judge_cfg.get("temperature", 0)
    api_url = _expand_env(judge_cfg.get("api_url", ""))
    api_key = _expand_env(judge_cfg.get("api_key", ""))
    stream_output = judge_cfg.get("output", "log") == "stream"
    if api_url:
        return OpenAILLMClient(model=model, api_url=api_url, api_key=api_key,
                               temperature=temperature, stream_output=stream_output)
    return AnthropicLLMClient(model=model, temperature=temperature, stream_output=stream_output)


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

    cfg = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f) or {}

    local_file = CONFIG_FILE.with_name("config.local.yaml")
    if local_file.exists():
        with open(local_file) as f:
            local = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, local)

    return cfg


def _expand_env(s: str) -> str:
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s)


def _expand_config(obj):
    """Recursively expand ${VAR} placeholders in all string values of a config dict."""
    if isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_config(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


@app.command()
def run(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Run specific scenario by ID"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Filter by tag"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Load scenarios without calling agent"),
    compare_with: Optional[str] = typer.Option(None, "--compare-with", help="Compare with run ID or 'last'"),
    agent_url: Optional[str] = typer.Option(None, "--agent-url", help="Override agent URL from config"),
    prompts: Optional[str] = typer.Option(None, "--prompts", help="Prompt resource URI (passed to agent CLI as --prompts)"),
    no_verify_ssl: bool = typer.Option(False, "--no-verify-ssl", help="Skip TLS certificate verification (corporate self-signed certs)"),
):
    """Run benchmark scenarios."""
    asyncio.run(_run_async(scenario, tag or [], dry_run, compare_with, agent_url, prompts, no_verify_ssl))


async def _run_async(
    scenario_id: str | None,
    tags: list[str],
    dry_run: bool,
    compare_with: str | None,
    agent_url_override: str | None,
    prompts_override: str | None = None,
    no_verify_ssl: bool = False,
):
    from bitbucket import build_proxy
    from runner.scenario_loader import load_scenarios
    from runner.judge import LLMJudge
    from runner.results_store import ResultsStore
    from runner.run import run_scenario

    cfg = _expand_config(_load_config())
    agent_cfg = cfg.get("agent", {})
    if agent_url_override:
        agent_cfg = {**agent_cfg, "base_url": agent_url_override}
    # Inject --prompts into CLI trigger command if provided
    if prompts_override and agent_cfg.get("trigger") == "cli":
        cmd = agent_cfg.get("command", "")
        if "--prompts" not in cmd:
            agent_cfg = {**agent_cfg, "command": cmd + f' --prompts="{prompts_override}"'}
    agent_url = agent_cfg.get("base_url", "http://localhost:8080")

    bitbucket_connection = cfg.get("bitbucket", {}).get("connection", {})
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

    trigger = _make_trigger(agent_cfg, bitbucket_connection)
    llm_client = _make_llm_client(judge_cfg)
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    _print_trigger_summary(agent_cfg, console)
    console.print(f"\n[bold]Running {len(scenarios)} scenario(s)...[/bold]\n")

    results = []
    for s in scenarios:
        bb_cfg = {**s.input["bitbucket"], "connection": bitbucket_connection, "verify_ssl": not no_verify_ssl}
        proxy = await build_proxy(bb_cfg)
        async with proxy:
            judge = LLMJudge(llm_client, proxy)
            result = await run_scenario(
                scenario=s,
                proxy=proxy,
                trigger=trigger,
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
        if result.verdict == "error" and result.error:
            console.print(f"   [red dim]{result.error}[/red dim]")

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
    html: bool = typer.Option(False, "--html", help="Generate HTML report and open it"),
):
    """Show report for a run."""
    from runner.results_store import ResultsStore

    cfg = _expand_config(_load_config())
    results_cfg = cfg.get("results", {})
    report_cfg = cfg.get("report", {})
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    if run_id == "last":
        run_list = store.list_runs(1)
        if not run_list:
            console.print("[red]No runs found.[/red]")
            raise typer.Exit(1)
        run_id = run_list[0]["run_id"]
        results = store.get_last_run()
    else:
        results = store.get_run_by_id(run_id)

    if not results:
        console.print("[red]Run not found.[/red]")
        raise typer.Exit(1)

    if html:
        from runner.html_report import generate
        import webbrowser
        output_dir = Path(report_cfg.get("output_dir", str(BASE_DIR / "reports"))).resolve()
        path = generate(run_id, results, output_dir)
        console.print(f"Report: [bold]{path}[/bold]")
        webbrowser.open(path.as_uri())
        return

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

    cfg = _expand_config(_load_config())
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
    no_verify_ssl: bool = typer.Option(False, "--no-verify-ssl", help="Skip TLS certificate verification (corporate self-signed certs)"),
):
    """A/B comparison of two agent versions."""
    asyncio.run(_ab_async(agent_a, agent_b, tag or [], scenario, no_verify_ssl))


async def _ab_async(agent_a: str, agent_b: str, tags: list[str], scenario_id: str | None, no_verify_ssl: bool = False):
    from bitbucket import build_proxy
    from runner.scenario_loader import load_scenarios
    from runner.judge import LLMJudge
    from runner.run import run_scenario

    cfg = _expand_config(_load_config())
    judge_cfg = cfg.get("judge", {})
    agent_cfg = cfg.get("agent", {})
    bitbucket_connection = cfg.get("bitbucket", {}).get("connection", {})

    scenarios = load_scenarios(SCENARIOS_DIR, tags=tags, scenario_id=scenario_id)
    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        raise typer.Exit(1)

    trigger_a = _make_trigger({**agent_cfg, "base_url": agent_a}, bitbucket_connection)
    trigger_b = _make_trigger({**agent_cfg, "base_url": agent_b}, bitbucket_connection)
    llm_client = _make_llm_client(judge_cfg)

    console.print(f"\n[bold]A/B test: {len(scenarios)} scenario(s)[/bold]")
    console.print(f"  Agent A: {agent_a}")
    console.print(f"  Agent B: {agent_b}\n")

    results_a, results_b = [], []
    for s in scenarios:
        console.print(f"Running {s.id}...")
        bb_cfg = {**s.input["bitbucket"], "connection": bitbucket_connection, "verify_ssl": not no_verify_ssl}
        proxy_a = await build_proxy(bb_cfg)
        async with proxy_a:
            ra = await run_scenario(s, proxy_a, trigger_a, LLMJudge(llm_client, proxy_a))
        proxy_b = await build_proxy(bb_cfg)
        async with proxy_b:
            rb = await run_scenario(s, proxy_b, trigger_b, LLMJudge(llm_client, proxy_b))
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
