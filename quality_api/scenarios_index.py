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
    """Locate the bench repo via quality_api.config — single source
    of truth for paths. Returns None if not found (caller decides
    whether that's fatal; for listing UI an empty result is fine)."""
    from . import config as _qa_config
    p = _qa_config.bench_repo()
    if p is None or not (p / "benchmark" / "scenarios").is_dir():
        return None
    return p


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
    """Return the fixture's bench_cmd with server-resolvable
    placeholders pre-expanded:
        {bench_root}   → quality_api.config.bench_repo()
        {fixture_path} → entry.path (absolute path to the yaml)
        {scenario_id}  → scenario_id
    Worker-side placeholders ({provider}, {scenario}, {lineage},
    {mutation}, {attempt}) are left intact for the worker to fill
    via str.format() at lease time.

    Empty bench_cmd → empty return (worker uses pool default)."""
    cmd = entry.bench_cmd or ""
    if not cmd:
        return ""
    from . import config as _qa_config
    bench = _qa_config.bench_repo()
    cmd = cmd.replace("{bench_root}", str(bench) if bench else "")
    cmd = cmd.replace("{fixture_path}", entry.path)
    cmd = cmd.replace("{scenario_id}", scenario_id)
    return cmd
