"""Yaml-driven schedule definitions.

A "schedule" is a saved auto-plan config — `(name, repo_path, branch_pattern,
providers, scenarios|scenario_tags, mode, …)` — that the discovery loop
fires on new commits. Today those live in `qa_auto_plan_configs` table,
created via UI or POST /api/qa/auto-plan/configs.

This module loads them from yaml files (under SCHEDULES_DIR, default
`<diff-graph>/schedules/`) and upserts into the DB on startup, making
yaml the source of truth. UI/CLI can still edit DB rows — those edits
get clobbered on next server restart if the row was yaml-imported (the
yaml "wins"). Rows created via UI without a yaml counterpart are
unaffected.

Yaml shape (one file per schedule, or one file with `schedules:` array):

    name:           diff-graph-unit-tier
    repo_path:      "${DIFFGRAPH_REPO_PATH}"   # env-substituted; empty → config default
    branch_pattern: master
    providers:      [deepseek]
    scenario_tags:  [tier:unit]
    # OR:
    # scenarios:    [REV-U-001-store-credit-concerns, INV-U-001-cancel-npe]
    attempts_min:   1
    pacing:         aggressive
    pacing_window_seconds: 0
    min_gap_seconds: 0
    mode:           auto                 # auto | on_demand
    enabled:        true
    bench_repo_path: "${BENCH_REPO_PATH}" # optional; falls back to config

Anywhere `${ENV}` appears in a path-like field, env-substitution runs;
unresolved env vars stay literal so they're visible in the UI.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class ScheduleDef:
    """Parsed yaml. Keys mirror AutoPlanStore.add_config kwargs."""
    name: str
    source_path: str            # absolute path to the yaml file
    repo_path: str = ""
    branch_pattern: str = "master"
    bench_repo_path: str = ""
    providers: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    scenario_tags: list[str] = field(default_factory=list)
    min_gap_seconds: int = 0
    pacing: str = "aggressive"
    pacing_window_seconds: int = 0
    attempts_min: int = 1
    enabled: bool = True
    mode: str = "auto"


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(s: str) -> str:
    """Substitute `${VAR}` from env; leave literal if unset."""
    if not isinstance(s, str):
        return s
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), s)


def _from_doc(doc: dict, source_path: str) -> Optional[ScheduleDef]:
    name = str(doc.get("name") or "").strip()
    if not name:
        log.warning("schedule yaml %s: missing `name`, skipping", source_path)
        return None
    return ScheduleDef(
        name=name,
        source_path=source_path,
        repo_path=_expand_env(str(doc.get("repo_path") or "")),
        branch_pattern=str(doc.get("branch_pattern") or "master"),
        bench_repo_path=_expand_env(str(doc.get("bench_repo_path") or "")),
        providers=[str(p) for p in (doc.get("providers") or [])],
        scenarios=[str(s) for s in (doc.get("scenarios") or [])],
        scenario_tags=[str(t) for t in (doc.get("scenario_tags") or [])],
        min_gap_seconds=int(doc.get("min_gap_seconds") or 0),
        pacing=str(doc.get("pacing") or "aggressive"),
        pacing_window_seconds=int(doc.get("pacing_window_seconds") or 0),
        attempts_min=int(doc.get("attempts_min") or 1),
        enabled=bool(doc.get("enabled", True)),
        mode=str(doc.get("mode") or "auto"),
    )


def _schedules_dir() -> Optional[Path]:
    """Resolution order: SCHEDULES_DIR env → <diff-graph>/schedules/.
    Returns None if neither exists."""
    env = os.environ.get("SCHEDULES_DIR", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    from . import config as _qa_config
    default = _qa_config.diffgraph_repo() / "schedules"
    return default if default.is_dir() else None


def load_files() -> list[ScheduleDef]:
    """Walk SCHEDULES_DIR recursively, parse every *.yaml. Supports
    single-doc and `schedules:` array forms. Skips drafts/."""
    out: list[ScheduleDef] = []
    d = _schedules_dir()
    if d is None:
        return out
    for p in sorted(d.rglob("*.yaml")):
        if "drafts" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
            doc = yaml.safe_load(text) or {}
        except Exception as e:
            log.warning("schedule yaml %s parse failed: %s", p, e)
            continue
        if isinstance(doc, dict) and "schedules" in doc:
            for sub in (doc.get("schedules") or []):
                if isinstance(sub, dict):
                    s = _from_doc(sub, str(p))
                    if s:
                        out.append(s)
        elif isinstance(doc, dict):
            s = _from_doc(doc, str(p))
            if s:
                out.append(s)
        else:
            log.warning("schedule yaml %s: unexpected top-level %s",
                        p, type(doc).__name__)
    return out


def upsert_into_db(defs: list[ScheduleDef], store: Any) -> dict:
    """For each ScheduleDef, find an existing config by name and
    update-in-place, or create new. Returns {created, updated,
    skipped, errors} counts + per-row notes."""
    created: list[str] = []
    updated: list[str] = []
    errors: list[dict] = []
    # Index existing configs by name.
    existing = {c.name: c for c in store.list_configs() if c.name}
    for d in defs:
        try:
            kwargs = dict(
                name=d.name,
                repo_path=d.repo_path or "",
                branch_pattern=d.branch_pattern,
                bench_repo_path=d.bench_repo_path or "",
                providers=d.providers,
                scenarios=d.scenarios,
                scenario_tags=d.scenario_tags,
                min_gap_seconds=d.min_gap_seconds,
                pacing=d.pacing,
                pacing_window_seconds=d.pacing_window_seconds,
                attempts_min=d.attempts_min,
                enabled=d.enabled,
                mode=d.mode,
            )
            if d.name in existing:
                cfg_id = existing[d.name].id
                # update_config takes ad-hoc fields; pass list fields raw
                store.update_config(cfg_id, **kwargs)
                updated.append(d.name)
            else:
                if not d.repo_path:
                    # repo_path is NOT NULL in DB; use the diff-graph repo
                    # as a safe default for yaml that didn't specify it.
                    from . import config as _qa_config
                    kwargs["repo_path"] = str(_qa_config.diffgraph_repo())
                store.add_config(**kwargs)
                created.append(d.name)
        except Exception as e:
            log.exception("schedule yaml upsert %s failed", d.name)
            errors.append({"name": d.name, "source": d.source_path,
                           "error": str(e)})
    return {
        "created": created, "updated": updated,
        "errors": errors,
        "schedules_dir": str(_schedules_dir() or ""),
        "total_defs": len(defs),
    }


def reload_all(store: Any) -> dict:
    """Single call: scan + upsert. Returns the upsert summary."""
    return upsert_into_db(load_files(), store)
