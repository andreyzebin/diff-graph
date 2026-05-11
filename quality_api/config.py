"""Server-side config resolution — replaces hardcoded paths.

Reads from env first (`BENCH_REPO_PATH`, `DIFFGRAPH_REPO_PATH`,
`QUALITY_PYTHON`), falls back to well-known dev layouts. One
module, every other quality_api / quality_cli site imports from
here — no `/home/andrey/...` literals anywhere else.

Override in production via:

    BENCH_REPO_PATH=/srv/bench
    DIFFGRAPH_REPO_PATH=/srv/diff-graph
    QUALITY_PYTHON=/srv/diff-graph/.venv/bin/python
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional


def _from_env(name: str) -> Optional[Path]:
    v = os.environ.get(name, "").strip()
    return Path(v).expanduser().resolve() if v else None


def _sibling(repo_dir: str) -> Optional[Path]:
    """Look for `<sibling>` next to this checkout — a common dev
    layout where repos sit in `~/repos/`."""
    here = Path(__file__).resolve().parents[2]   # ~/repos
    cand = here / repo_dir
    return cand if cand.exists() else None


@lru_cache(maxsize=1)
def diffgraph_repo() -> Path:
    """Root of this diff-graph checkout."""
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def bench_repo() -> Optional[Path]:
    """Root of the bench (code-review-benchmarks) checkout.
    Returns None if not found — callers decide whether that's fatal.
    """
    return (_from_env("BENCH_REPO_PATH")
            or _sibling("code-review-benchmarks"))


@lru_cache(maxsize=1)
def python_executable() -> str:
    """Python interpreter to use for spawned subprocesses (workers,
    cli.py, bench). Honours QUALITY_PYTHON; defaults to the venv
    sitting next to diff-graph's repo."""
    env = os.environ.get("QUALITY_PYTHON", "").strip()
    if env:
        return env
    venv_py = diffgraph_repo() / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return "python"  # PATH fallback


def default_bench_cmd_template() -> str:
    """Template for the worker's bench subprocess. {scenario},
    {provider}, etc. are filled in by the worker via .format()."""
    bench = bench_repo()
    if bench is None:
        # No bench root resolved — return a template that will
        # fail loudly so the operator knows to set BENCH_REPO_PATH.
        return ("echo 'BENCH_REPO_PATH not set / bench repo not found' "
                "&& exit 64")
    return (
        f"cd {bench} && source .env "
        f"&& unset ALL_PROXY all_proxy "
        f"&& .venv/bin/python benchmark/cli.py run -s {{scenario}} -p {{provider}}"
    )


def quality_cli_path() -> str:
    """Command that launches `quality_cli` (the worker / search /
    tasks CLI). Used by the pool supervisor when spawning workers."""
    return f"{python_executable()} -m quality_cli"


def server_url(default: str = "http://localhost:8765") -> str:
    """Origin the local QA server listens on. Used by CLI's
    --open-in-ui flag + by code that needs to build deep-links."""
    return os.environ.get("QA_SERVER_URL", default).rstrip("/")
