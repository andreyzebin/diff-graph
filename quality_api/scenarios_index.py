"""Scenario discovery — recursive walk of bench's scenarios/ dir.

Used by /api/qa/scenarios (UI list) and by /api/qa/fire-anonymous
(scenario-id → fixture-path resolution + per-scenario bench_cmd
detection: unit-tier fixtures contain `repo:` and need
`bench run-unit <path>` instead of the integration runner).

No on-disk index — scan is fast enough on each request and the
fixtures change rarely. If discoverability ever becomes a hot
path we can add a watcher.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class ScenarioEntry:
    id: str
    path: str               # absolute path to the yaml
    rel_path: str           # relative to scenarios/ dir
    agent: str = ""         # dispatcher | reviewer | investigator | '' (from yaml.agent)
    tags: list[str] = field(default_factory=list)
    title: str = ""
    bench_cmd: str = ""     # per-fixture cmd override (yaml.bench_cmd). Empty
                            # falls through to the worker pool's default cmd.


def _bench_root() -> Optional[Path]:
    """Locate the bench repo. Env var wins; fall back to the most
    common dev layout. If we can't find it, return None — caller
    decides what to do (typically: empty list)."""
    env = os.environ.get("BENCH_REPO_PATH", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return p if (p / "benchmark" / "scenarios").is_dir() else None
    # Common dev fallback — sibling of diff-graph
    here = Path(__file__).resolve().parents[2]  # repos/
    cand = here / "code-review-benchmarks"
    if (cand / "benchmark" / "scenarios").is_dir():
        return cand
    return None


def list_scenarios() -> list[ScenarioEntry]:
    """Recursive scan of <bench>/benchmark/scenarios/. Skips drafts/.

    Yaml is the source of truth: `id`, `agent`, `tags`, `bench_cmd`
    all come from the file. No path-based classification — "tier"
    is just a tag (e.g. `tier:unit`) like any other, no special
    handling in code.
    """
    root = _bench_root()
    if root is None:
        return []
    scenarios_dir = root / "benchmark" / "scenarios"
    out: list[ScenarioEntry] = []
    for p in sorted(scenarios_dir.rglob("*.yaml")):
        rel = p.relative_to(scenarios_dir)
        if "drafts" in rel.parts or "fixtures" in rel.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
            # Multi-doc yaml: take first non-empty.
            doc = next((d for d in yaml.safe_load_all(text) if d), None) or {}
        except Exception as e:
            log.warning("scenario load failed: %s: %s", p, e)
            continue
        if not isinstance(doc, dict):
            continue
        out.append(ScenarioEntry(
            id=str(doc.get("id") or p.stem),
            path=str(p),
            rel_path=str(rel),
            agent=str(doc.get("agent") or ""),
            tags=[str(t) for t in (doc.get("tags") or [])],
            title=str(doc.get("name") or doc.get("title") or ""),
            bench_cmd=str(doc.get("bench_cmd") or "").strip(),
        ))
    return out


def find_scenario(scenario_id: str) -> Optional[ScenarioEntry]:
    """Lookup by id. Errors on duplicate ids — caller must disambiguate."""
    all_ = list_scenarios()
    matches = [s for s in all_ if s.id == scenario_id]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"scenario id '{scenario_id}' is ambiguous: "
            f"{', '.join(s.rel_path for s in matches)}"
        )
    return matches[0]


def build_bench_cmd(entry: ScenarioEntry, *, scenario_id: str) -> str:
    """Return whatever bench_cmd template the fixture's yaml
    declares — empty means "use the worker pool's default cmd".
    No code-side logic about tiers; the yaml decides."""
    return entry.bench_cmd or ""
