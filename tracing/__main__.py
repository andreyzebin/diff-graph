"""
Tracing CLI: python -m tracing metrics --hash X
"""
from __future__ import annotations

import argparse
import json
import sys

from .query import get_metrics, get_runs, compare


def main():
    parser = argparse.ArgumentParser(prog="tracing", description="Trace DB query tool")
    sub = parser.add_subparsers(dest="command")

    # metrics
    p = sub.add_parser("metrics", help="Aggregate metrics for a prompt hash")
    p.add_argument("--hash", required=True, help="Prompt hash")
    p.add_argument("--since", help="Start date (ISO format)")
    p.add_argument("--db", help="DB path override")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # runs
    p = sub.add_parser("runs", help="List runs for a prompt hash")
    p.add_argument("--hash", required=True, help="Prompt hash")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--db", help="DB path override")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # compare
    p = sub.add_parser("compare", help="Compare two prompt hashes")
    p.add_argument("--a", required=True, help="Hash A")
    p.add_argument("--b", required=True, help="Hash B")
    p.add_argument("--db", help="DB path override")
    p.add_argument("--format", default="text", choices=["text", "json"])

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    db_kwargs = {"db_path": args.db} if args.db else {}

    if args.command == "metrics":
        m = get_metrics(args.hash, since=args.since, **db_kwargs)
        if args.format == "json":
            print(json.dumps(m.to_dict(), indent=2))
        else:
            for k, v in m.to_dict().items():
                print(f"  {k}: {v}")

    elif args.command == "runs":
        runs = get_runs(args.hash, limit=args.limit, **db_kwargs)
        if args.format == "json":
            print(json.dumps(runs, indent=2))
        else:
            for r in runs:
                print(f"  {r['id'][:12]}  {(r.get('started_at') or '?')[:19]}  "
                      f"findings={r.get('findings_count', 0)}  "
                      f"tokens={r.get('total_tokens_paid', 0)}  "
                      f"{r.get('status', '?')}")

    elif args.command == "compare":
        c = compare(args.a, args.b, **db_kwargs)
        if args.format == "json":
            print(json.dumps(c.to_dict(), indent=2))
        else:
            print(f"  A ({args.a}): {c.a.runs_count} runs")
            print(f"  B ({args.b}): {c.b.runs_count} runs")
            print()
            for metric, d in c.delta.items():
                arrow = "↑" if d["diff"] > 0 else "↓" if d["diff"] < 0 else "="
                print(f"  {metric:25s}  A={d['a']:>8}  B={d['b']:>8}  {arrow} {d['diff_pct']:+.1f}%")
            if c.p_value is not None:
                sig = "significant" if c.p_value < 0.05 else "not significant"
                print(f"\n  p-value: {c.p_value} ({sig})")


if __name__ == "__main__":
    main()
