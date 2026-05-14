"""Repo-root conftest — sys.path wiring for the merged tree.

The `benchmarks/` subtree (was the `code-review-benchmarks` repo) runs
its code with `benchmarks/` itself on `sys.path` — `benchmarks/cli.py`
does `sys.path.insert(0, BASE_DIR)` at import time so its modules
import each other as `from runner.X` / `from bitbucket.X` rather than
`from benchmarks.runner.X`.

That insert only fires when `benchmarks.cli` is imported, which does
NOT happen during plain test collection of `benchmarks/tests/*`. So
without help, `benchmarks/tests/test_*.py` doing `from runner.run_unit
import ...` fails at collection time when `pytest` is run from the
repo root.

Adding `benchmarks/` to `sys.path` here — once, before collection —
keeps the bench tests importing exactly as they did when the bench
was its own repo. The engine has no top-level `runner` / `bitbucket`
module, so there's no shadowing risk.
"""
import sys
from pathlib import Path

_BENCHMARKS = Path(__file__).parent / "benchmarks"
if _BENCHMARKS.is_dir():
    p = str(_BENCHMARKS)
    if p not in sys.path:
        sys.path.insert(0, p)
