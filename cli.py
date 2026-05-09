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
import json
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
        # Optional second template for interaction scenarios (/help,
        # /ask, unknown command). When a scenario triggers via a posted
        # comment, the runner uses this template with {message} and
        # {comment_id} placeholders; cli.py's dispatcher path picks it
        # up. Falls back to `command` if missing.
        interaction_command = agent_cfg.get("interaction_command", "") or None
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
            interaction_command_template=interaction_command,
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
    extra_body = judge_cfg.get("extra_body") or None
    timeout = judge_cfg.get("timeout")
    if api_url:
        return OpenAILLMClient(model=model, api_url=api_url, api_key=api_key,
                               temperature=temperature, stream_output=stream_output,
                               extra_body=extra_body, timeout=timeout)
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
    provider: Optional[list[str]] = typer.Option(None, "--provider", "-p", help="LLM provider profile to pass to the agent CLI (repeatable). Without it, the agent uses its own config."),
    all_providers: bool = typer.Option(False, "--all-providers", help="Run scenarios against every provider listed in agent.providers (config.local.yaml)."),
    repeat: int = typer.Option(1, "--repeat", "-n", help="Run each scenario N times and aggregate (median score, union of warnings). Useful for variance-prone agents/judges."),
    mode: str = typer.Option("gentle", "--mode", help="Run mode: 'gentle' (sequential, polite to shared LLM endpoints — default) or 'aggressive' (bounded-parallel via temp-branch PRs, fast pre-merge smoke)."),
    max_per_provider: int = typer.Option(2, "--max-per-provider", help="Aggressive mode: max concurrent tasks PER PROVIDER. The bottleneck is the LLM endpoint, not the bench, so the budget is per-model — total in-flight = N × providers. Default 2."),
):
    """Run benchmark scenarios."""
    if mode not in ("gentle", "aggressive"):
        console.print(f"[red]invalid --mode {mode!r}; use gentle or aggressive[/red]")
        raise typer.Exit(2)
    asyncio.run(_run_async(scenario, tag or [], dry_run, compare_with, agent_url, prompts, no_verify_ssl,
                           list(provider or []), all_providers, repeat, mode, max_per_provider))


_PROVIDER_FLAG_RE = __import__("re").compile(r"\s*--provider[= ]\{provider\}")


def _next_attempt_dir(parent: Path) -> Path:
    """parent/attempt-NN where NN = max(existing) + 1, starting at 01."""
    parent.mkdir(parents=True, exist_ok=True)
    existing = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith("attempt-")]
    n = 1
    if existing:
        nums = []
        for p in existing:
            try:
                nums.append(int(p.name.split("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        if nums:
            n = max(nums) + 1
    d = parent / f"attempt-{n:02d}"
    d.mkdir()
    return d


def _safe_seg(s: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "unnamed"


# Channels whose verdict depends on the agent's tool-call log
# (invocations.json). For these, the bench MUST capture invocations
# during the run — without them the judge has no input and silently
# scores 0.
_INVOCATION_DEPENDENT_CHANNELS = frozenset({"intended_concerns", "intended_findings"})


def _scenario_needs_invocations(s) -> bool:
    """True when the bench must write invocations.json for the attempt.

    Two reasons:
      1. agent-isolation knobs in setup/trigger (mocks, custom agent,
         user-message override) — the original case.
      2. assert_via includes intended_concerns / intended_findings —
         the judge will read invocations.json to extract reflect /
         done(findings) data.
    """
    if (s.setup.mocks_path
            or s.trigger.agent
            or s.trigger.data
            or s.trigger.user_message_path
            or s.trigger.user_message):
        return True
    return any(c in _INVOCATION_DEPENDENT_CHANNELS for c in s.expected_output.assert_via)


def _git_sha(path: Path) -> str:
    """Best-effort git rev-parse HEAD. Empty string on failure."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip()
    except Exception:
        return ""


def _inject_provider(cmd: str, provider: str | None) -> str:
    """Substitute {provider} or strip the flag entirely when no provider is chosen.

    With provider:    --provider={provider}  ->  --provider=deepseek
    Without provider: drop "--provider={provider}" so the agent uses its own config.
    """
    if provider is None:
        return _PROVIDER_FLAG_RE.sub("", cmd)
    if "{provider}" in cmd:
        return cmd.replace("{provider}", provider)
    return cmd + f' --provider={provider}'


async def _run_async(
    scenario_id: str | None,
    tags: list[str],
    dry_run: bool,
    compare_with: str | None,
    agent_url_override: str | None,
    prompts_override: str | None = None,
    no_verify_ssl: bool = False,
    providers: list[str] | None = None,
    all_providers: bool = False,
    repeat: int = 1,
    mode: str = "gentle",
    max_per_provider: int = 2,
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

    # Resolve providers: explicit list > --all-providers (from config) > [None] (no-op pass-through)
    providers = list(providers or [])
    if all_providers:
        providers = list(agent_cfg.get("providers", []))
        if not providers:
            console.print("[yellow]--all-providers given but agent.providers is empty in config[/yellow]")
            raise typer.Exit(1)
    if not providers:
        providers = [None]   # single run, agent uses its own config

    scenarios = load_scenarios(SCENARIOS_DIR, tags=tags, scenario_id=scenario_id)

    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        raise typer.Exit(1)

    if dry_run:
        console.print(f"\n[bold]Dry run — {len(scenarios)} scenario(s) × {len(providers)} provider(s):[/bold]\n")
        for s in scenarios:
            req = len(s.expected_output.required_comments)
            console.print(
                f"  [cyan]{s.id:12}[/cyan] {s.name}  "
                f"[dim]{', '.join(s.tags)}  required={req}[/dim]"
            )
        for p in providers:
            console.print(f"  provider: [magenta]{p or '(default)'}[/magenta]")
        raise typer.Exit(0)

    llm_client = _make_llm_client(judge_cfg)
    store = ResultsStore(
        store_path=Path(results_cfg.get("store_path", str(RESULTS_DIR))),
        db_path=Path(results_cfg.get("db_path", str(RESULTS_DIR / "benchmark.db"))),
    )

    # ── Session dir layout ────────────────────────────────────────────
    # Set BENCHMARK_TRACE_DIR (or pass --trace-dir) to enable. Inside:
    #   <session-id>/
    #     bench.json
    #     summary.json
    #     <provider>/<scenario>/attempt-NN/
    #       agent/      <- DIFFGRAPH_TRACE_PATH points here for the subprocess
    #       judge/      <- judge writes request/response/error here
    #       result.json <- final score & verdict for this attempt
    bench_root = os.environ.get("BENCHMARK_TRACE_DIR")
    # Auto-promote a temp BENCHMARK_TRACE_DIR when the run will need
    # invocations.json (intended_* channels or agent-isolation knobs)
    # but the user didn't set one. Without this, the judge silently
    # gets an empty invocations log and scores those attempts at 0.
    if not bench_root and any(_scenario_needs_invocations(s) for s in scenarios):
        import tempfile as _tempfile
        bench_root = _tempfile.mkdtemp(prefix="bench-traces-")
        console.print(
            f"[yellow]BENCHMARK_TRACE_DIR not set; using temp: {bench_root}[/yellow]\n"
            f"[dim](required because some scenarios assert via intended_findings / "
            f"intended_concerns or agent-isolation knobs — set BENCHMARK_TRACE_DIR "
            f"to keep traces.)[/dim]"
        )
    session_dir: Path | None = None
    if bench_root:
        label = os.environ.get("BENCH_LABEL", "")
        ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        session_id = f"{ts}-{_safe_seg(label)}" if label else ts
        session_dir = Path(bench_root).expanduser() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        # Capture identity of the agent under test (best effort).
        agent_repo_path = Path(agent_cfg.get("cwd") or ".").expanduser()
        if not agent_repo_path.is_absolute():
            agent_repo_path = Path.cwd() / agent_repo_path
        # Try to extract diff-graph repo path from `cd <path> && ...` template.
        cmd_str = agent_cfg.get("command", "")
        cd_match = __import__("re").search(r"cd\s+(\S+)", cmd_str)
        if cd_match:
            agent_repo_path = Path(cd_match.group(1)).expanduser()
        bench_meta = {
            "session_id": session_id,
            "started_at": datetime.datetime.utcnow().isoformat(),
            "label": label,
            "providers": [p or "(default)" for p in providers],
            "scenarios": [s.id for s in scenarios],
            "agent": {
                "command": cmd_str,
                "cwd": agent_cfg.get("cwd", ""),
                "git_sha": _git_sha(agent_repo_path),
                "repo_path": str(agent_repo_path),
            },
            "judge": {
                "model": judge_cfg.get("model", ""),
                "api_url": judge_cfg.get("api_url", ""),
            },
        }
        (session_dir / "bench.json").write_text(
            json.dumps(bench_meta, ensure_ascii=False, indent=2),
        )
        console.print(f"[dim]Session: {session_dir}[/dim]")

    matrix: dict[str, list] = {}    # provider -> list[ScenarioResult]
    results: list = []              # flat list across all providers (for store.save_run)
    summary_rows: list[dict] = []

    # Build per-provider trigger objects up front (provider gets baked
    # into the command template at this stage). Reused across all
    # (scenario × attempt) tasks for that provider.
    prov_triggers: dict[str, tuple] = {}
    for prov in providers:
        prov_label = prov or "(default)"
        run_agent_cfg = dict(agent_cfg)
        if run_agent_cfg.get("trigger") == "cli":
            run_agent_cfg["command"] = _inject_provider(run_agent_cfg.get("command", ""), prov)
            if run_agent_cfg.get("interaction_command"):
                run_agent_cfg["interaction_command"] = _inject_provider(
                    run_agent_cfg["interaction_command"], prov,
                )
        console.print(f"\n[bold magenta]── provider: {prov_label} ─────────────────────────────────────[/bold magenta]")
        _print_trigger_summary(run_agent_cfg, console)
        prov_triggers[prov_label] = (prov, _make_trigger(run_agent_cfg, bitbucket_connection))

    if mode == "aggressive":
        console.print(f"\n[bold]Running {len(scenarios) * max(1, repeat) * len(providers)} task(s) — aggressive (max-per-provider={max_per_provider}; total in-flight ≤ {max_per_provider * len(providers)}) ─[/bold]\n")
    else:
        console.print(f"\n[bold]Running {len(scenarios)} scenario(s) × {len(providers)} provider(s) × {max(1, repeat)} attempt(s) — gentle (sequential) ─[/bold]\n")

    # Per-attempt unit: returns (result, attempt_dir, agent_dir).
    # Self-contained — no shared os.environ mutation, so safe to run
    # in parallel under asyncio.Semaphore.
    async def _run_one_attempt(prov_label, prov, trigger, s, attempt_idx):
        attempt_dir: Path | None = None
        agent_dir: Path | None = None
        judge_dir: Path | None = None
        invocations_path: Path | None = None
        env_overrides: dict[str, str] = {}
        if session_dir is not None:
            sc_parent = session_dir / _safe_seg(prov_label) / _safe_seg(s.id)
            attempt_dir = _next_attempt_dir(sc_parent)
            # Homogeneous trace layout (TODO §5e.10a):
            #   attempt-NN/runs/agent/  ← DIFFGRAPH_TRACE_PATH
            #   attempt-NN/runs/judge/  ← LLMJudge writes here via TraceFSWriter
            #   attempt-NN/runs/agent/invocations.json
            # Agent and judge get the same scaffolding so the same
            # tooling (read_file walks, tree views, debugger sub-agents)
            # works for both.
            runs_root = attempt_dir / "runs"
            agent_dir = runs_root / "agent"
            judge_dir = runs_root / "judge"
            agent_dir.mkdir(parents=True)
            judge_dir.mkdir(parents=True)
            env_overrides["DIFFGRAPH_TRACE_PATH"] = str(agent_dir)
            # Tag the run row in trace DB with bench-specific search
            # dimensions (TODO §5e.11). diff-graph's cli.py reads these
            # env vars and writes them onto runs.scenario_id /
            # runs.scenario_tags so dashboards can filter
            # "all REV-001 runs" or "all tier:unit runs".
            env_overrides["DIFFGRAPH_SCENARIO_ID"] = s.id
            if s.tags:
                env_overrides["DIFFGRAPH_SCENARIO_TAGS"] = ",".join(s.tags)
            # When the scenario opts into agent-isolation features or
            # asserts via intended_concerns / intended_findings, have
            # the agent write its tool invocations log next to the
            # attempt artefacts so the judge can pick it up.
            if _scenario_needs_invocations(s):
                invocations_path = agent_dir / "invocations.json"

        bb_cfg = {**s.input["bitbucket"], "connection": bitbucket_connection, "verify_ssl": not no_verify_ssl}
        result = None
        proxy = None
        try:
            proxy = await build_proxy(bb_cfg)
            async with proxy:
                judge = LLMJudge(
                    llm_client, proxy,
                    judge_dir=judge_dir,
                    model=judge_cfg.get("model", ""),
                    verdict_source=judge_cfg.get("verdict_source", "api"),
                    scenario_id=s.id,
                    scenario_tags=list(s.tags or []),
                )
                result = await run_scenario(
                    scenario=s,
                    proxy=proxy,
                    trigger=trigger,
                    judge=judge,
                    env_overrides=env_overrides,
                    invocations_out=invocations_path,
                )
        except Exception as exc:
            from runner.scorer import ScenarioResult
            err_msg = f"{type(exc).__name__}: {exc}"
            console.print(f"   [red dim]{prov_label}/{s.id} attempt {attempt_idx + 1} failed: {err_msg}[/red dim]")
            result = ScenarioResult(
                scenario_id=s.id,
                scenario_name=s.name,
                verdict="error",
                score=0.0,
                required_found=0,
                required_total=len(s.expected_output.required_comments),
                false_positives=0,
                location_accuracy=0.0,
                status_change_verdict="n/a",
                inline_ratio=0.0,
                total_comments=0,
                duration_seconds=0.0,
                judge_summary=err_msg,
                error=err_msg,
                pr_url=getattr(proxy, "pr_url", None) if proxy else None,
            )

        _generation = ""
        _mutation = ""
        if agent_dir is not None and (agent_dir / "run.json").exists():
            try:
                _agent_run = json.loads((agent_dir / "run.json").read_text())
                _generation = _agent_run.get("prompt_source", "") or ""
                if _generation and "/" in _generation:
                    _generation = _generation.rsplit("/", 1)[-1]
                _mutation = _agent_run.get("prompt_hash", "") or ""
            except Exception:
                pass
        if attempt_dir is not None:
            (attempt_dir / "result.json").write_text(json.dumps({
                "scenario": s.id,
                "provider": prov_label,
                "attempt": attempt_dir.name,
                "verdict": result.verdict,
                "score": result.score,
                "comments": result.total_comments,
                "duration_seconds": result.duration_seconds,
                "error": result.error,
                "generation": _generation,
                "mutation": _mutation,
            }, ensure_ascii=False, indent=2))
            summary_rows.append({
                "provider": prov_label, "scenario": s.id,
                "attempt": attempt_dir.name, "score": result.score,
                "verdict": result.verdict, "comments": result.total_comments,
                "duration_seconds": result.duration_seconds,
                "generation": _generation, "mutation": _mutation,
                "path": str(attempt_dir.relative_to(session_dir)),
            })

        # Progressive verdict line — print as soon as each attempt
        # finishes (not after the whole asyncio.gather completes).
        # Final per-provider summary still prints at the end in
        # deterministic (provider × scenario) order; this is the
        # in-flight feed only.
        if result.verdict == "pass":
            icon = "[green]✅[/green]"
        elif result.verdict == "error":
            icon = "[red]❌[/red]"
        else:
            icon = "[yellow]⚠️ [/yellow]"
        attempt_tag = ""
        if max(1, repeat) > 1:
            attempt_tag = f" #{attempt_idx + 1}/{max(1, repeat)}"
        console.print(
            f"{icon} [magenta]{prov_label:14}[/magenta] {s.id:12} "
            f"{s.name[:36]:36} score=[bold]{result.score:.2f}[/bold]  "
            f"{result.duration_seconds:.1f}s{attempt_tag}"
        )

        return prov_label, s.id, attempt_idx, (result, attempt_dir, agent_dir)

    # Build all units (provider × scenario × attempt) and run them
    # under the chosen mode.
    units = []
    for prov_label, (prov, trigger) in prov_triggers.items():
        for s in scenarios:
            for attempt_idx in range(max(1, repeat)):
                units.append((prov_label, prov, trigger, s, attempt_idx))

    if mode == "aggressive" and max_per_provider > 0 and len(units) > 1:
        # Per-provider semaphores: the LLM endpoint is the real
        # bottleneck, so each provider gets its own concurrency budget
        # and they run side-by-side. Total in-flight is at most
        # max_per_provider × len(providers); within one provider we
        # never exceed max_per_provider.
        sems: dict[str, asyncio.Semaphore] = {
            label: asyncio.Semaphore(max_per_provider)
            for label in prov_triggers
        }
        async def _bound(u):
            prov_label = u[0]
            async with sems[prov_label]:
                return await _run_one_attempt(*u)
        unit_results = await asyncio.gather(*(_bound(u) for u in units))
    else:
        unit_results = []
        for u in units:
            unit_results.append(await _run_one_attempt(*u))

    # Group by (prov_label, scenario_id) for per-pair aggregation.
    by_pair: dict[tuple, list] = {}
    for prov_label, scen_id, _attempt_idx, attempt_data in unit_results:
        by_pair.setdefault((prov_label, scen_id), []).append(attempt_data)

    # Iterate in the original (provider × scenario) order so the
    # printed summary stays deterministic regardless of how aggressive
    # mode interleaved the actual execution.
    from runner.scorer import aggregate_results
    for prov in providers:
        prov_label = prov or "(default)"
        prov_results: list = []
        for s in scenarios:
            attempts = by_pair.get((prov_label, s.id), [])
            attempt_only = [r for r, _, _ in attempts]
            if not attempt_only:
                continue
            result = aggregate_results(attempt_only)
            attempt_dir = attempts[-1][1]
            agent_dir = attempts[-1][2]

            prov_results.append(result)
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
            score_disp = f"[bold]{result.score:.2f}[/bold]"
            if result.attempts and result.score_min is not None and result.score_max is not None:
                score_disp = (
                    f"[bold]{result.score:.2f}[/bold] "
                    f"[dim]median over {len(result.attempts)} "
                    f"({result.score_min:.2f}..{result.score_max:.2f})[/dim]"
                )
            console.print(
                f"{icon} [magenta]{prov_label:14}[/magenta] {s.id:12} {s.name[:38]:38} "
                f"score={score_disp}  "
                f"comments={result.total_comments}  "
                f"{result.duration_seconds:.1f}s"
                f"{fail_reason}"
            )
            if result.verdict == "error" and result.error:
                console.print(f"   [red dim]{result.error}[/red dim]")
            warnings = (result.judge_output.scenario_warnings
                        if result.judge_output else [])
            for w in warnings:
                console.print(
                    f"   [yellow]⚠ scenario:[/yellow] [bold]{w.kind}[/bold] — {w.detail}"
                )
            agent_warnings = (result.judge_output.agent_warnings
                              if result.judge_output else [])
            for w in agent_warnings:
                cid = f" #{w.comment_id}" if w.comment_id else ""
                console.print(
                    f"   [magenta]⚠ agent{cid}:[/magenta] [bold]{w.kind}[/bold] — {w.detail}"
                )

        matrix[prov_label] = prov_results

    # ── Per-provider summaries + cross-provider matrix ─────────────────
    console.print(f"\n{'─' * 70}")
    for prov_label, prov_results in matrix.items():
        prov_passed = sum(1 for r in prov_results if r.passed)
        prov_avg = sum(r.score for r in prov_results) / max(len(prov_results), 1)
        prov_total = sum(r.duration_seconds for r in prov_results)
        console.print(
            f"[bold magenta]{prov_label:24}[/bold magenta] "
            f"{prov_passed}/{len(prov_results)} passed   "
            f"avg_score={prov_avg:.2f}   total={prov_total:.1f}s"
        )

    passed = sum(1 for r in results if r.passed)
    avg_score = sum(r.score for r in results) / len(results)
    total_time = sum(r.duration_seconds for r in results)
    console.print(f"{'─' * 70}")
    console.print(
        f"[bold]Overall : {passed}/{len(results)} passed   "
        f"avg_score={avg_score:.2f}   total={total_time:.1f}s[/bold]"
    )
    flagged = [
        (r.scenario_id, w)
        for r in results
        if r.judge_output
        for w in r.judge_output.scenario_warnings
    ]
    if flagged:
        console.print(
            f"[yellow]Scenario warnings: {len(flagged)} across "
            f"{len({sid for sid, _ in flagged})} scenario(s)[/yellow]"
        )
        for sid, w in flagged:
            console.print(
                f"  [yellow]⚠[/yellow] {sid}: [bold]{w.kind}[/bold] — {w.detail}"
            )
    agent_flagged = [
        (r.scenario_id, w)
        for r in results
        if r.judge_output
        for w in r.judge_output.agent_warnings
    ]
    if agent_flagged:
        console.print(
            f"[magenta]Agent reasoning warnings: {len(agent_flagged)} across "
            f"{len({sid for sid, _ in agent_flagged})} scenario(s)[/magenta]"
        )
        for sid, w in agent_flagged:
            cid = f" #{w.comment_id}" if w.comment_id else ""
            console.print(
                f"  [magenta]⚠[/magenta] {sid}{cid}: [bold]{w.kind}[/bold] — {w.detail}"
            )
    seen_generations = sorted({r.get("generation", "") for r in summary_rows if r.get("generation")})
    seen_mutations = sorted({r.get("mutation", "") for r in summary_rows if r.get("mutation")})
    if seen_generations or seen_mutations:
        gen_str = ", ".join(seen_generations) or "(none)"
        mut_str = ", ".join(seen_mutations) or "(none)"
        console.print(f"Prompts : generation=[cyan]{gen_str}[/cyan]  mutation=[cyan]{mut_str}[/cyan]")

    # Write the bench session summary alongside bench.json.
    if session_dir is not None:
        # Collect distinct prompt generations/mutations seen across the run.
        # Usually one each; multiple values mean attempts were inconsistent
        # (e.g. caller passed --prompts to some routes and not others).
        generations = sorted({r["generation"] for r in summary_rows if r.get("generation")})
        mutations = sorted({r["mutation"] for r in summary_rows if r.get("mutation")})
        summary = {
            "session_id": session_dir.name,
            "finished_at": datetime.datetime.utcnow().isoformat(),
            "prompts": {
                "generations": generations,
                "mutations": mutations,
            },
            "totals": {
                "passed": passed,
                "total": len(results),
                "avg_score": avg_score,
                "total_seconds": total_time,
            },
            "by_provider": {
                p: {
                    "passed": sum(1 for r in rs if r.passed),
                    "total": len(rs),
                    "avg_score": sum(r.score for r in rs) / max(len(rs), 1),
                    "total_seconds": sum(r.duration_seconds for r in rs),
                } for p, rs in matrix.items()
            },
            "rows": summary_rows,
        }
        (session_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
        )
        console.print(f"[dim]Session summary: {session_dir / 'summary.json'}[/dim]")

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
