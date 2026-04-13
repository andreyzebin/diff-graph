"""
Evolution CLI: python -m evolution status|measure|benchmark|tree
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .models import EvolutionConfig
from .connectors import TracingConnector, AnalyticsConnector, BenchmarkConnector, WebhookConnector
from .population import Population


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="evolution", description="Evolution manager for prompt generations")
    parser.add_argument("--state", help="State file path override")
    sub = parser.add_subparsers(dest="command")

    # init
    p = sub.add_parser("init", help="Initialize main branch")
    p.add_argument("--prompts", required=True, help="Prompt path or URI")
    p.add_argument("--hash", default="", help="Prompt hash (auto-computed if empty)")

    # status
    sub.add_parser("status", help="Show population status")

    # tree
    sub.add_parser("tree", help="Show phylogenetic tree")

    # measure
    p = sub.add_parser("measure", help="Collect metrics for a branch")
    p.add_argument("--branch", default="main", help="Branch ID")

    # measure-all
    sub.add_parser("measure-all", help="Collect metrics for all active branches")

    # benchmark
    p = sub.add_parser("benchmark", help="Run benchmark for a branch")
    p.add_argument("--branch", default="main", help="Branch ID")

    # compare
    p = sub.add_parser("compare", help="Compare two branches")
    p.add_argument("--a", required=True, help="Branch A")
    p.add_argument("--b", required=True, help="Branch B")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Build population with connectors
    from pathlib import Path
    state_file = Path(args.state) if args.state else None
    pop = _build_population(state_file)

    if args.command == "init":
        prompt_hash = args.hash
        if not prompt_hash:
            # Compute from files
            from orchestra.compiler import compile_prompts
            reg = compile_prompts(args.prompts, use_cache=False)
            prompt_hash = reg.source_hash or ""
        branch = pop.init_main(args.prompts, prompt_hash)
        print(f"Initialized main: {branch.prompt_ref} hash={branch.prompt_hash[:12]}")

    elif args.command == "status":
        s = pop.status()
        print(json.dumps(s, indent=2))

    elif args.command == "tree":
        print(pop.tree())

    elif args.command == "measure":
        m = pop.measure(args.branch)
        print(json.dumps(m.to_dict(), indent=2))

    elif args.command == "measure-all":
        results = pop.measure_all()
        for branch_id, m in results.items():
            print(f"\n{branch_id}:")
            for k, v in m.to_dict().items():
                if k not in ("branch_id", "timestamp", "by_capability"):
                    print(f"  {k}: {v}")

    elif args.command == "benchmark":
        print(f"Running benchmark for {args.branch}...")
        result = pop.run_benchmark(args.branch)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "compare":
        a = pop.measurements.get(args.a)
        b = pop.measurements.get(args.b)
        if not a or not b:
            print("Run 'measure' for both branches first")
            sys.exit(1)
        print(f"{'metric':25s}  {'A':>8}  {'B':>8}  {'diff':>8}")
        print("-" * 55)
        for field in ["fitness", "benchmark_score", "acceptance_rate", "tokens_per_finding", "steps_avg"]:
            va = getattr(a, field, None)
            vb = getattr(b, field, None)
            if va is not None and vb is not None:
                diff = vb - va
                print(f"{field:25s}  {va:>8.3f}  {vb:>8.3f}  {diff:>+8.3f}")


def _build_population(state_file=None) -> Population:
    """Build population with default connectors."""
    import os
    kwargs = {}
    if state_file:
        kwargs["state_file"] = state_file

    analytics_db = os.environ.get("EVOLUTION_ANALYTICS_DB", "")
    benchmark_cmd = os.environ.get("EVOLUTION_BENCHMARK_CMD", "")
    benchmark_cwd = os.environ.get("EVOLUTION_BENCHMARK_CWD", "")
    webhook_url = os.environ.get("EVOLUTION_WEBHOOK_URL", "http://localhost:8000")

    return Population(
        tracing=TracingConnector(),
        analytics=AnalyticsConnector(db_path=analytics_db),
        benchmark=BenchmarkConnector(cmd=benchmark_cmd, cwd=benchmark_cwd),
        webhook=WebhookConnector(url=webhook_url),
        **kwargs,
    )


if __name__ == "__main__":
    main()
