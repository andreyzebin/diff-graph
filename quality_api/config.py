"""Server-side config resolution — replaces hardcoded paths.

Reads from env first (`DIFFGRAPH_REPO_PATH`, `QUALITY_PYTHON`),
falls back to well-known dev layouts. One module, every other
quality_api / quality_cli site imports from here — no
`/home/andrey/...` literals anywhere else.

The bench (`benchmarks/`) now lives INSIDE this repo, so
`bench_repo()` is just `diffgraph_repo()` — no separate checkout,
no `BENCH_REPO_PATH` env. The function is kept as a named seam so
callers that conceptually mean "the bench root" stay readable, and
so a future split-out can re-introduce the env override in one place.

Override in production via:

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


@lru_cache(maxsize=1)
def diffgraph_repo() -> Path:
    """Root of this diff-graph checkout."""
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def bench_repo() -> Path:
    """Root of the bench tree. The bench (`benchmarks/`) is a
    subtree of this repo, so this is just `diffgraph_repo()`.
    Kept as a named function so callers reading "the bench root"
    stay self-documenting and a future split-out has one seam."""
    return diffgraph_repo()


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
    {provider}, etc. are filled in by the worker via .format().
    Runs from the repo root — `benchmarks/` is a subtree here, so
    the bench CLI is just `benchmarks/cli.py` and it shares the
    repo's single `.venv` + `.env`."""
    return (
        f"cd {diffgraph_repo()} && source .env "
        f"&& unset ALL_PROXY all_proxy "
        f"&& .venv/bin/python benchmarks/cli.py run -s {{scenario}} -p {{provider}}"
    )


def quality_cli_path() -> str:
    """Command that launches `quality_cli` (the worker / search /
    tasks CLI). Used by the pool supervisor when spawning workers."""
    return f"{python_executable()} -m quality_cli"


def server_url(default: str = "http://localhost:8765") -> str:
    """Origin the local QA server listens on. Used by CLI's
    --open-in-ui flag + by code that needs to build deep-links."""
    return os.environ.get("QA_SERVER_URL", default).rstrip("/")
