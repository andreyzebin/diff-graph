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
    tier: str = ""          # 'unit' | 'integration' | '' (from scenario_tags tier:X)
    agent: str = ""         # dispatcher | reviewer | investigator | '' (from path)
    tags: list[str] = field(default_factory=list)
    title: str = ""
    is_unit_fixture: bool = False   # has `repo:` block → run via bench run-unit


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


def _classify_path(rel: str) -> str:
    """`unit/reviewer/REV-U-001.yaml` → 'reviewer'.
    `agents/reviewer/REV-001.yaml` → 'reviewer'.
    `java/SCEN-009.yaml` → '' (no agent layer)."""
    parts = rel.split(os.sep)
    for token in ("reviewer", "investigator", "dispatcher"):
        if token in parts:
            return token
    return ""


def _classify_tier(parts: list[str], tags: list[str]) -> str:
    if any(t == "tier:unit" or t.startswith("tier:unit") for t in tags):
        return "unit"
    if any(t == "tier:integration" or t.startswith("tier:integration") for t in tags):
        return "integration"
    if "unit" in parts:
        return "unit"
    if "agents" in parts:
        return "unit"     # bench's agent-isolation scenarios live under agents/
    return "integration"


def list_scenarios() -> list[ScenarioEntry]:
    """Recursive scan of <bench>/benchmark/scenarios/. Skips drafts/."""
    root = _bench_root()
    if root is None:
        return []
    scenarios_dir = root / "benchmark" / "scenarios"
    out: list[ScenarioEntry] = []
    for p in sorted(scenarios_dir.rglob("*.yaml")):
        rel = p.relative_to(scenarios_dir)
        rel_parts = rel.parts
        if "drafts" in rel_parts or "fixtures" in rel_parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
            # Accept multi-doc yaml — we just want the first doc.
            doc = next((d for d in yaml.safe_load_all(text) if d), None) or {}
        except Exception as e:
            log.warning("scenario load failed: %s: %s", p, e)
            continue
        if not isinstance(doc, dict):
            continue
        sid = str(doc.get("id") or p.stem)
        tags = list(doc.get("tags") or [])
        agent = str(doc.get("agent") or "") or _classify_path(str(rel))
        tier = _classify_tier(list(rel_parts), [str(t) for t in tags])
        is_unit = ("repo" in doc) and isinstance(doc.get("repo"), dict)
        out.append(ScenarioEntry(
            id=sid,
            path=str(p),
            rel_path=str(rel),
            tier=tier,
            agent=agent,
            tags=[str(t) for t in tags],
            title=str(doc.get("name") or doc.get("title") or ""),
            is_unit_fixture=is_unit,
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
    """Per-scenario bench_cmd. Unit fixtures use `bench run-unit
    <yaml-path>` (the runner handles temp clone + fake PR /
    spawn-block / sink). Integration scenarios fall back to the
    pool's bench_cmd template (caller's responsibility).

    Matches DEFAULT_BENCH_CMD's shell prelude: `source .env` loads
    DEEPSEEK_API_KEY / NO_PROXY / certs / etc.; `unset ALL_PROXY
    all_proxy` strips the SOCKS proxy URL that httpx can't parse
    (httpx wants `socks5://`, not bare `socks://`)."""
    if not entry.is_unit_fixture:
        return ""
    root = _bench_root()
    if root is None:
        return ""
    return (
        f"cd {root} && source .env "
        f"&& unset ALL_PROXY all_proxy "
        f"&& .venv/bin/python -m benchmark.cli run-unit "
        f"{entry.path} --provider={{provider}}"
    )
