"""Auto-plan discovery — git branches → QA plans, open-config edition.

Configs are now "open": each describes WHAT to run (filter by
explicit scenario list and/or scenario tags) and HOW often / how
aggressively. Multiple parallel configs coexist — typical setup
is one config for sentinel-on-every-commit and another for
full-slice-once-a-day, but the system doesn't enforce that
shape.

Concepts (TODO §5e.11+):
- lineage        = a long-lived line of git commits we're testing
                   as a hypothesis (typically a git branch like
                   `master` or `feature/foo`)
- mutation       = a specific commit on that lineage we benched
- config         = description of what to plan + cadence + pacing
- planned_commit = idempotency ledger row, keyed by (config × lineage
                   × sha × kind=config_name)

Crash-resilience: all state in SQLite. discover() is idempotent —
two consecutive calls with the same repo state and the same
configs produce zero new plans the second time.

Cadence:
- min_gap_seconds=0   → fire on every new commit (default for unit)
- min_gap_seconds=N   → debounce per lineage — at most one plan per
                        lineage per N seconds (typical for full
                        runs that are too expensive to run on every
                        push). Implemented by checking the latest
                        qa_planned_commits row for this (config,
                        lineage).

Pacing:
- aggressive          → all tasks enqueued with not_before=NULL,
                        ready to lease immediately (default)
- spread              → tasks staggered across pacing_window_seconds
                        via not_before. e.g. 100 tasks / 3600s ⇒
                        each task is delayed +36s from the previous
                        within a single plan. The lease query honours
                        not_before so workers naturally pace.
"""
from __future__ import annotations

import fnmatch
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .queue import PlanSpec, PlanStore, TaskQueue, TaskSpec


# ── Config row ──────────────────────────────────────────────────────────────

@dataclass
class AutoPlanConfig:
    id: int
    name: str
    repo_path: str                       # repo to watch (git branches × HEAD shas)
    branch_pattern: str                  # CSV of globs — git-side pattern matched against actual git branches
    bench_repo_path: str                 # where benchmark/scenarios/*.yaml lives
    providers: list[str]
    scenarios: list[str]                 # explicit scenario ids
    scenario_tags: list[str]             # tag filter (resolved at discover-time)
    min_gap_seconds: int                 # debounce per lineage
    pacing: str                          # 'aggressive' | 'spread'
    pacing_window_seconds: int
    attempts_min: int
    enabled: bool
    created_at: str
    last_discover_at: Optional[str]
    mode: str = "auto"                   # 'auto' | 'on_demand'


def _row_to_config(row: sqlite3.Row | None) -> Optional[AutoPlanConfig]:
    if row is None:
        return None

    def _arr(s):
        try:
            return json.loads(s) if s else []
        except Exception:
            return []

    def _col(name, default=None):
        try:
            v = row[name]
            return v if v is not None else default
        except (KeyError, IndexError):
            return default

    return AutoPlanConfig(
        id=int(row["id"]),
        name=row["name"] or "",
        repo_path=row["repo_path"],
        branch_pattern=row["branch_pattern"],
        bench_repo_path=_col("bench_repo_path", "") or "",
        providers=_arr(row["providers"]),
        scenarios=_arr(_col("scenarios")),
        scenario_tags=_arr(_col("scenario_tags")),
        min_gap_seconds=int(_col("min_gap_seconds", 0) or 0),
        pacing=(_col("pacing", "aggressive") or "aggressive"),
        pacing_window_seconds=int(_col("pacing_window_seconds", 0) or 0),
        attempts_min=int(row["attempts_min"] or 1),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        last_discover_at=row["last_discover_at"],
        mode=(_col("mode", "auto") or "auto"),
    )


def config_to_dict(c: AutoPlanConfig) -> dict:
    return {
        "id": c.id, "name": c.name,
        "repo_path": c.repo_path, "branch_pattern": c.branch_pattern,
        "bench_repo_path": c.bench_repo_path,
        "providers": c.providers,
        "scenarios": c.scenarios,
        "scenario_tags": c.scenario_tags,
        "min_gap_seconds": c.min_gap_seconds,
        "pacing": c.pacing,
        "pacing_window_seconds": c.pacing_window_seconds,
        "attempts_min": c.attempts_min,
        "enabled": c.enabled,
        "created_at": c.created_at,
        "last_discover_at": c.last_discover_at,
        "mode": c.mode,
    }


# ── Git helpers ─────────────────────────────────────────────────────────────

def git_list_branches(repo_path: str) -> list[tuple[str, str]]:
    p = Path(repo_path).expanduser()
    if not (p / ".git").exists() and not p.name == ".git":
        return []
    try:
        out = subprocess.check_output(
            ["git", "-C", str(p), "for-each-ref",
             "--format=%(refname:short) %(objectname)", "refs/heads/"],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except Exception:
        return []
    pairs = []
    for line in out.splitlines():
        if " " not in line:
            continue
        b, sha = line.strip().split(" ", 1)
        pairs.append((b.strip(), sha.strip()))
    return pairs


def match_any(branch: str, patterns_csv: str) -> bool:
    """Match a git branch name against the config's branch_pattern globs.
    The parameter name stays `branch` because this helper operates on
    raw git-side branch strings (the `branch_pattern` exception)."""
    if not patterns_csv:
        return False
    return any(fnmatch.fnmatch(branch, p.strip()) for p in patterns_csv.split(",") if p.strip())


# ── Scenario resolver ──────────────────────────────────────────────────────

def _resolve_bench_root(bench_repo_path: str) -> Optional[Path]:
    """Resolve the bench checkout root, falling back to
    quality_api.config when the field is empty or carries an
    unsubstituted `${ENV}` placeholder."""
    if bench_repo_path and "${" not in bench_repo_path:
        p = Path(bench_repo_path).expanduser()
        if (p / "benchmark" / "scenarios").is_dir():
            return p
    from . import config as _qa_config
    return _qa_config.bench_repo()


def scan_scenarios(bench_repo_path: str) -> list[dict]:
    """Walk <bench_repo>/benchmark/scenarios/**/*.yaml and return
    [{id, tags: [...]}, ...]. Used to resolve scenario_tags filters
    at discover-time.

    Best-effort: missing or malformed YAML is silently skipped.
    """
    bench = _resolve_bench_root(bench_repo_path)
    if bench is None:
        return []
    root = bench / "benchmark" / "scenarios"
    if not root.exists():
        return []
    try:
        import yaml
    except ImportError:
        return []
    out = []
    for path in root.rglob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            sid = data.get("id")
            tags = data.get("tags") or []
            if sid:
                out.append({"id": str(sid), "tags": [str(t) for t in tags],
                            "path": str(path.relative_to(root))})
        except Exception:
            continue
    return out


def resolve_scenarios(cfg: AutoPlanConfig) -> list[str]:
    """Compute the actual scenario id list this config matches right now.

    - If `scenarios` is non-empty → use it verbatim (explicit list wins).
    - Else if `scenario_tags` non-empty → match scenarios in
      bench_repo whose tag set INTERSECTS the filter.
    - Else → empty (no-op config; planner skips).

    Tag intersect semantics: `scenario_tags = [tier:unit]` matches any
    scenario whose `tags:` list contains `tier:unit`. Multiple tags
    OR together (any match counts).
    """
    if cfg.scenarios:
        return list(cfg.scenarios)
    if not cfg.scenario_tags:
        return []
    catalogue = scan_scenarios(cfg.bench_repo_path)
    wanted = set(cfg.scenario_tags)
    return [s["id"] for s in catalogue if wanted & set(s.get("tags", []))]


# ── Store ───────────────────────────────────────────────────────────────────

class AutoPlanStore:
    def __init__(self, queue: TaskQueue):
        self.queue = queue
        self.plans = PlanStore(queue)

    # ── CRUD ──────────────────────────────────────────────────────────

    def add_config(self, *,
                   name: str = "",
                   repo_path: str,
                   branch_pattern: str,
                   bench_repo_path: str = "",
                   providers: list[str],
                   scenarios: list[str] | None = None,
                   scenario_tags: list[str] | None = None,
                   min_gap_seconds: int = 0,
                   pacing: str = "aggressive",
                   pacing_window_seconds: int = 0,
                   attempts_min: int = 1,
                   enabled: bool = True,
                   mode: str = "auto") -> int:
        if not providers:
            raise ValueError("providers required")
        scenarios = scenarios or []
        scenario_tags = scenario_tags or []
        if not scenarios and not scenario_tags:
            raise ValueError("either scenarios or scenario_tags must be set")
        if pacing not in ("aggressive", "spread"):
            raise ValueError(f"pacing must be 'aggressive' or 'spread', got {pacing}")
        if mode not in ("auto", "on_demand"):
            raise ValueError(f"mode must be 'auto' or 'on_demand', got {mode}")
        repo_path = str(Path(repo_path).expanduser().resolve())
        if bench_repo_path:
            bench_repo_path = str(Path(bench_repo_path).expanduser().resolve())

        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_auto_plan_configs
                   (name, repo_path, branch_pattern, bench_repo_path,
                    providers, scenarios, scenario_tags,
                    min_gap_seconds, pacing, pacing_window_seconds,
                    attempts_min, enabled, mode, created_at,
                    -- legacy NOT-NULL columns: keep them satisfied with empty arrays.
                    unit_scenarios, full_scenarios, full_period_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?)""",
                (name or "", repo_path, branch_pattern, bench_repo_path,
                 json.dumps(providers, ensure_ascii=False),
                 json.dumps(scenarios, ensure_ascii=False),
                 json.dumps(scenario_tags, ensure_ascii=False),
                 int(min_gap_seconds), pacing, int(pacing_window_seconds),
                 int(attempts_min), 1 if enabled else 0,
                 mode, datetime.now(timezone.utc).isoformat(),
                 "[]", "[]", 86400),
            )
            c.commit()
            return int(cur.lastrowid)

    def update_config(self, config_id: int, **fields) -> bool:
        """PATCH-style update. Only known columns are touched.
        Pass JSON-serialisable values for list fields (providers /
        scenarios / scenario_tags); the method serialises.
        """
        allowed = {
            "name", "repo_path", "branch_pattern", "bench_repo_path",
            "providers", "scenarios", "scenario_tags",
            "min_gap_seconds", "pacing", "pacing_window_seconds",
            "attempts_min", "enabled", "mode",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k in ("providers", "scenarios", "scenario_tags"):
                v = json.dumps(list(v), ensure_ascii=False)
            elif k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return False
        params.append(config_id)
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                f"UPDATE qa_auto_plan_configs SET {', '.join(sets)} WHERE id=?",
                params,
            )
            c.commit()
            return cur.rowcount > 0

    def list_configs(self, *, only_enabled: bool = False) -> list[AutoPlanConfig]:
        clause = "WHERE enabled=1" if only_enabled else ""
        with self.queue._lock, self.queue._conn() as c:
            rows = c.execute(
                f"SELECT * FROM qa_auto_plan_configs {clause} ORDER BY id"
            ).fetchall()
        return [_row_to_config(r) for r in rows if _row_to_config(r) is not None]

    def get_config(self, config_id: int) -> Optional[AutoPlanConfig]:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                "SELECT * FROM qa_auto_plan_configs WHERE id=?", (config_id,)
            ).fetchone()
        return _row_to_config(row)

    def set_enabled(self, config_id: int, enabled: bool) -> bool:
        return self.update_config(config_id, enabled=enabled)

    def delete_config(self, config_id: int) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute("DELETE FROM qa_auto_plan_configs WHERE id=?", (config_id,))
            c.commit()
            return cur.rowcount > 0

    def resolve_config_scenarios(self, config_id: int) -> list[str]:
        """Helper for the UI to preview which scenarios match right now."""
        cfg = self.get_config(config_id)
        return resolve_scenarios(cfg) if cfg else []

    # ── Discovery ─────────────────────────────────────────────────────

    def discover(self, *, config_id: Optional[int] = None) -> list[dict]:
        configs = (
            [self.get_config(config_id)] if config_id is not None
            else self.list_configs(only_enabled=True)
        )
        configs = [c for c in configs if c is not None]
        # mode='on_demand' configs don't fire on auto discovery — they
        # only run when explicitly triggered via fire_on(). The discovery
        # supervisor calls discover() without config_id; we filter here.
        configs = [c for c in configs if (c.mode or "auto") == "auto"]
        created = []
        for cfg in configs:
            for plan in self._discover_one(cfg):
                created.append(plan)
            with self.queue._lock, self.queue._conn() as c:
                c.execute(
                    "UPDATE qa_auto_plan_configs SET last_discover_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), cfg.id),
                )
                c.commit()
        return created

    def fire_on(self, config_id: int, *, lineage: str, sha: str) -> Optional[dict]:
        """Manually fire a config against a specific (lineage, sha).

        For `auto` mode: idempotent via qa_planned_commits so a discovery
        loop doesn't re-plan an already-processed commit.
        For `on_demand` mode: always proceeds. The user clicked ▶ fire
        deliberately — they want another sample (e.g. to grow the
        scoring dataset). The ledger entry is upserted via INSERT OR
        REPLACE so the latest plan_id is the one recorded.
        """
        cfg = self.get_config(config_id)
        if cfg is None or not cfg.enabled:
            return None
        if cfg.mode != "on_demand" and self._already_planned(config_id, lineage, sha):
            return None
        scenarios = resolve_scenarios(cfg)
        if not scenarios:
            return None
        plan_id, task_ids = self._create_plan(cfg, lineage, sha, scenarios)
        self._record_planned(config_id, lineage, sha,
                             cfg.name or f"config-{config_id}", plan_id,
                             upsert=(cfg.mode == "on_demand"))
        return {
            "config_id": config_id, "config_name": cfg.name,
            "lineage": lineage, "sha": sha,
            "plan_id": plan_id, "task_count": len(task_ids),
        }

    def _discover_one(self, cfg: AutoPlanConfig) -> list[dict]:
        out: list[dict] = []
        scenarios = resolve_scenarios(cfg)
        if not scenarios:
            return out
        # Resolve repo_path: empty / unresolved `${ENV}` → diff-graph
        # checkout from quality_api.config (single source of truth).
        repo_path = cfg.repo_path
        if not repo_path or "${" in repo_path:
            from . import config as _qa_config
            repo_path = str(_qa_config.diffgraph_repo())
        # Each git branch matched by branch_pattern becomes a lineage —
        # a long-lived line of git commits we'll bench against.
        lineages = [(b, sha) for (b, sha) in git_list_branches(repo_path)
                    if match_any(b, cfg.branch_pattern)]

        for lineage, sha in lineages:
            # Skip if this exact (config, lineage, sha) was already planned —
            # idempotent across crashes.
            if self._already_planned(cfg.id, lineage, sha):
                continue
            # Debounce: skip if there's a recent plan for THIS lineage
            # within min_gap_seconds (regardless of sha).
            if cfg.min_gap_seconds > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=cfg.min_gap_seconds)).isoformat()
                if self._has_recent_plan(cfg.id, lineage, cutoff):
                    continue

            plan_id, task_ids = self._create_plan(cfg, lineage, sha, scenarios)
            self._record_planned(cfg.id, lineage, sha, cfg.name or f"config-{cfg.id}", plan_id)
            out.append({
                "config_id": cfg.id, "config_name": cfg.name,
                "lineage": lineage, "sha": sha,
                "plan_id": plan_id, "task_count": len(task_ids),
            })
        return out

    def _create_plan(self, cfg: AutoPlanConfig, lineage: str, sha: str,
                     scenarios: list[str]) -> tuple[int, list[int]]:
        """Fan out tasks for this (config, lineage, sha) cell.

        With pacing='spread', tasks within the cell are staggered
        using not_before so workers don't blast through the LLM
        endpoint all at once.
        """
        plan_name = f"auto:{cfg.name or cfg.id}:{lineage}@{sha[:7]}"
        # Build the spec without the cross-product defaults; we manage
        # the fan-out manually here so we can apply pacing.
        with self.queue._lock, self.queue._conn() as c:
            cur = c.execute(
                """INSERT INTO qa_plans
                   (name, created_at, created_by, lineages, providers,
                    scenarios, attempts_min, state, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (plan_name, datetime.now(timezone.utc).isoformat(),
                 f"auto-plan/config-{cfg.id}",
                 json.dumps([lineage]),
                 json.dumps(cfg.providers),
                 json.dumps(scenarios),
                 max(1, cfg.attempts_min),
                 f"repo={cfg.repo_path}"),
            )
            plan_id = int(cur.lastrowid)
            c.commit()

        total_cells = len(cfg.providers) * len(scenarios) * max(1, cfg.attempts_min)
        spread_step = (
            cfg.pacing_window_seconds / max(1, total_cells)
            if cfg.pacing == "spread" and cfg.pacing_window_seconds > 0
            else 0
        )
        now = datetime.now(timezone.utc)

        task_ids = []
        slot_idx = 0
        for queue_name in cfg.providers:
            for scenario in scenarios:
                for attempt_n in range(1, max(1, cfg.attempts_min) + 1):
                    not_before = None
                    if spread_step > 0:
                        not_before = (now + timedelta(seconds=spread_step * slot_idx)).isoformat()
                    # Stage B: scenario/lineage/mutation flow as URIs.
                    resources = [
                        f"scenario://{scenario}",
                        f"lineage://{lineage}",
                        f"mutation://{sha}",
                    ]
                    payload_base = {"plan_name": plan_name,
                                    "config_id": cfg.id,
                                    "config_name": cfg.name}
                    agent_tid = self.queue.enqueue(TaskSpec(
                        queue=queue_name,
                        attempt_n=attempt_n,
                        plan_id=plan_id,
                        priority=100,
                        payload=dict(payload_base),
                        resources=list(resources),
                        not_before=not_before,
                    ))
                    task_ids.append(agent_tid)
                    judge_tid = self.queue.enqueue(TaskSpec(
                        queue=queue_name,
                        attempt_n=attempt_n,
                        plan_id=plan_id,
                        priority=100,
                        kind="judge",
                        parent_task_id=agent_tid,
                        initial_state="blocked",
                        payload=dict(payload_base),
                        resources=list(resources),
                    ))
                    task_ids.append(judge_tid)
                    slot_idx += 1
        return plan_id, task_ids

    # ── Idempotency ledger ────────────────────────────────────────────

    def _already_planned(self, config_id: int, lineage: str, sha: str) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                """SELECT 1 FROM qa_planned_commits
                   WHERE config_id=? AND lineage=? AND sha=?""",
                (config_id, lineage, sha),
            ).fetchone()
        return row is not None

    def _has_recent_plan(self, config_id: int, lineage: str, since_iso: str) -> bool:
        with self.queue._lock, self.queue._conn() as c:
            row = c.execute(
                """SELECT 1 FROM qa_planned_commits
                   WHERE config_id=? AND lineage=? AND planned_at >= ?""",
                (config_id, lineage, since_iso),
            ).fetchone()
        return row is not None

    def _record_planned(self, config_id: int, lineage: str,
                        sha: str, kind: str, plan_id: int,
                        *, upsert: bool = False) -> None:
        verb = "INSERT OR REPLACE" if upsert else "INSERT OR IGNORE"
        with self.queue._lock, self.queue._conn() as c:
            c.execute(
                f"""{verb} INTO qa_planned_commits
                    (config_id, lineage, sha, plan_kind, plan_id, planned_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (config_id, lineage, sha, kind, plan_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            c.commit()


# ── Server-side periodic discovery ──────────────────────────────────────────

class DiscoverySupervisor:
    """Background asyncio task that runs AutoPlanStore.discover() on a
    timer. Mirror of pools.WorkerSupervisor — same lifecycle hooks,
    same DB-as-truth contract.

    Symmetric story:
      pools     supervisor → spawns workers when queue has work
      schedules supervisor → creates plans (which fill the queue) when
                              new commits appear

    Together they make the auto-plan loop run hands-free: a `git push`
    that updates a watched lineage produces a planned commit on the
    next discovery tick, which spawns a worker on the next pool tick,
    which leases tasks and runs the bench.
    """

    def __init__(self, store: AutoPlanStore, *, interval_seconds: int = 60):
        self.store = store
        self.interval = max(15, int(interval_seconds))
        import asyncio
        self._stop = asyncio.Event()
        self._task = None

    def start(self) -> None:
        import asyncio
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        import asyncio
        self._stop.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self):
        import asyncio, logging
        log = logging.getLogger(__name__)
        while not self._stop.is_set():
            try:
                created = await asyncio.to_thread(self.store.discover)
                if created:
                    log.info("discovery: created %d plan(s)", len(created))
            except Exception:
                log.exception("discovery tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval,
                )
            except asyncio.TimeoutError:
                pass
