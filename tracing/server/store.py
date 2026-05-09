"""Storage abstraction over the trace DB (and, in a follow-up,
filesystem trace dumps).

Phase 1 of the search API (TODO §5e.11). Implements the SQLite
backend with rich filtering across the new search-dimension columns
added by orchestra/trace_db.py:

  agent_name, kind, generation, mutation, genes (JSON array),
  project, files_touched, jira_keys, scenario_id, scenario_tags,
  linked_run_id, duration_ms, fs_trace_path

The abstraction is small on purpose — most queries are one or two
SQL statements. The point isn't to be a query DSL; it's to have a
single seam where a future FilesystemTraceStore can plug in (5e.10a,
secondary FS-only viewer).

Design notes:
- All filters are optional; absence = "any".
- Multi-value filters (?gene=X&gene=Y) are AND by default for genes
  / tags / files_touched / jira_keys (all listed must be present).
  ?gene_any=X|Y is OR.
- Time filters use ISO-8601 strings to keep the API stable across
  timezones.
- Pagination via offset for now; cursor is in the design backlog.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from orchestra.trace_db import DEFAULT_DB_PATH


# ── Filter spec ──────────────────────────────────────────────────────────────

@dataclass
class RunFilter:
    """All filters for the /api/runs list endpoint. Every field is optional."""
    # By run attributes
    kind: Optional[str] = None
    agent_name: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    since: Optional[str] = None         # ISO datetime
    until: Optional[str] = None         # ISO datetime
    duration_gt_ms: Optional[int] = None
    tokens_gt: Optional[int] = None

    # By evolutionary identity
    generation: Optional[str] = None
    mutation: Optional[str] = None      # exact or prefix
    genes: list[str] = field(default_factory=list)         # all listed (AND)
    genes_any: list[str] = field(default_factory=list)     # any listed (OR)
    without_gene: list[str] = field(default_factory=list)  # NOT contains

    # By work object
    pr_url: Optional[str] = None
    project: Optional[str] = None
    file: Optional[str] = None          # files_touched contains
    jira: Optional[str] = None          # jira_keys contains
    scenario_id: Optional[str] = None
    scenario_tag: Optional[str] = None  # scenario_tags contains

    # By relationship
    linked_run: Optional[str] = None

    # Pagination & sort
    limit: int = 50
    offset: int = 0
    sort: str = "started_at"            # column name
    order: str = "desc"                 # asc | desc


@dataclass
class ToolCallFilter:
    """Filters for /api/tool_calls — cross-run search of agent_tool_request events."""
    tool: Optional[str] = None
    agent_name: Optional[str] = None
    model: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    args_contains: Optional[str] = None  # plain substring in args JSON
    limit: int = 50
    offset: int = 0


# ── Store ────────────────────────────────────────────────────────────────────

class SQLiteTraceStore:
    """Read-only adapter over orchestra's traces.db.

    Queries assume the schema produced by orchestra/trace_db.py
    after the §5e.11 migration. Older DBs without the new columns
    will get NULL rows for those fields — queries that filter on
    them return empty, which is the correct behaviour ("you have
    no runs tagged with gene X because this DB pre-dates genes").
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    # ── Connection helper ──────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            # Empty store — caller should treat as zero-runs.
            raise FileNotFoundError(f"trace DB not found: {self.db_path}")
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    # ── List / search runs ─────────────────────────────────────────────

    def list_runs(self, f: RunFilter) -> list[dict]:
        where, params = self._where_for_runs(f)
        sort_col = self._safe_sort_col(f.sort)
        order = "DESC" if (f.order or "desc").lower() == "desc" else "ASC"
        q = f"""
            SELECT id, kind, agent_name, model, status,
                   started_at, finished_at, duration_ms,
                   pr_url, project, scenario_id, scenario_tags,
                   generation, mutation, genes, files_touched, jira_keys,
                   linked_run_id, fs_trace_path,
                   findings_count, total_tokens_paid, prompt_source, prompt_hash
            FROM runs
            WHERE {where}
            ORDER BY {sort_col} {order}
            LIMIT ? OFFSET ?
        """
        params.extend([f.limit, f.offset])
        try:
            with self._conn() as c:
                rows = c.execute(q, params).fetchall()
        except FileNotFoundError:
            return []
        return [self._row_to_dict(r) for r in rows]

    def count_runs(self, f: RunFilter) -> int:
        where, params = self._where_for_runs(f)
        q = f"SELECT COUNT(*) FROM runs WHERE {where}"
        try:
            with self._conn() as c:
                return int(c.execute(q, params).fetchone()[0])
        except FileNotFoundError:
            return 0

    def get_run(self, run_id: str) -> Optional[dict]:
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
        except FileNotFoundError:
            return None
        return self._row_to_dict(row) if row else None

    # ── Tool-call search (cross-run) ────────────────────────────────────

    def search_tool_calls(self, f: ToolCallFilter) -> list[dict]:
        """Return tool requests across all runs, joined with their run row.

        Each result carries enough run context (model, agent, scenario)
        that the agent / human caller doesn't need a second round-trip.
        """
        clauses = ["e.event_type = 'agent_tool_request'"]
        params: list[Any] = []
        if f.tool:
            clauses.append("json_extract(e.data_json, '$.tool') = ?")
            params.append(f.tool)
        if f.args_contains:
            clauses.append("e.data_json LIKE ?")
            params.append(f"%{f.args_contains}%")
        if f.agent_name:
            clauses.append("e.agent_name = ?")
            params.append(f.agent_name)
        if f.since:
            clauses.append("e.timestamp >= ?")
            params.append(f.since)
        if f.until:
            clauses.append("e.timestamp <= ?")
            params.append(f.until)
        if f.model:
            clauses.append("r.model = ?")
            params.append(f.model)
        where = " AND ".join(clauses)
        q = f"""
            SELECT e.id, e.run_id, e.agent_name, e.step, e.timestamp,
                   e.data_json,
                   r.kind, r.model, r.scenario_id, r.mutation, r.generation,
                   r.fs_trace_path
            FROM events e
            LEFT JOIN runs r ON r.id = e.run_id
            WHERE {where}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([f.limit, f.offset])
        try:
            with self._conn() as c:
                rows = c.execute(q, params).fetchall()
        except FileNotFoundError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            # Unpack args from data_json for ergonomic consumer access.
            try:
                data = json.loads(d.get("data_json") or "{}")
                d["tool"] = data.get("tool")
                d["args"] = data.get("args")
            except Exception:
                pass
            out.append(d)
        return out

    # ── Genes ───────────────────────────────────────────────────────────

    def list_genes(self) -> list[dict]:
        """Catalogue: each gene with run counts.

        Reads runs.genes (JSON array) via json_each and tallies.
        Genes that have never been seen on any run row are absent —
        that's correct behaviour; the catalogue lists what's
        actually been observed in this DB.
        """
        q = """
            SELECT json_each.value AS gene, COUNT(*) AS runs_count
            FROM runs, json_each(runs.genes)
            WHERE runs.genes IS NOT NULL AND runs.genes != ''
            GROUP BY json_each.value
            ORDER BY runs_count DESC
        """
        try:
            with self._conn() as c:
                rows = c.execute(q).fetchall()
        except FileNotFoundError:
            return []
        return [{"gene": r["gene"], "runs_count": r["runs_count"]} for r in rows]

    # ── Aggregates ──────────────────────────────────────────────────────

    def list_dimensions(self) -> dict:
        """Distinct values for each filter dimension — feeds the
        dropdowns on /qa/runs. One round-trip instead of six."""
        out = {
            "kind": [], "agent_name": [], "model": [],
            "scenario_id": [], "generation": [], "project": [],
            "scenario_tags": [], "genes": [], "status": [],
        }
        try:
            with self._conn() as c:
                # Scalar columns: SELECT DISTINCT
                for col in ("kind", "agent_name", "model", "scenario_id",
                            "generation", "project", "status"):
                    rows = c.execute(
                        f"SELECT DISTINCT {col} AS v FROM runs "
                        f"WHERE {col} IS NOT NULL AND {col} != '' "
                        f"ORDER BY v"
                    ).fetchall()
                    out[col] = [r["v"] for r in rows]
                # JSON-array columns: json_each + DISTINCT
                for col in ("scenario_tags", "genes"):
                    rows = c.execute(
                        f"SELECT DISTINCT json_each.value AS v "
                        f"FROM runs, json_each(runs.{col}) "
                        f"WHERE runs.{col} IS NOT NULL AND runs.{col} != '' "
                        f"ORDER BY v"
                    ).fetchall()
                    out[col] = [r["v"] for r in rows]
        except FileNotFoundError:
            pass
        return out

    def aggregate_by_provider(self, f: RunFilter | None = None) -> list[dict]:
        f = f or RunFilter(limit=10**9, offset=0)
        where, params = self._where_for_runs(f)
        q = f"""
            SELECT model,
                   COUNT(*)                                AS runs,
                   AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_duration_ms,
                   AVG(CASE WHEN total_tokens_paid IS NOT NULL THEN total_tokens_paid END) AS avg_tokens,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)     AS errored
            FROM runs
            WHERE {where} AND model IS NOT NULL AND model != ''
            GROUP BY model
            ORDER BY runs DESC
        """
        try:
            with self._conn() as c:
                rows = c.execute(q, params).fetchall()
        except FileNotFoundError:
            return []
        return [dict(r) for r in rows]

    def aggregate_by_scenario(self, f: RunFilter | None = None) -> list[dict]:
        f = f or RunFilter(limit=10**9, offset=0)
        where, params = self._where_for_runs(f)
        q = f"""
            SELECT scenario_id,
                   COUNT(*)                                AS runs,
                   AVG(duration_ms)                        AS avg_duration_ms,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
            FROM runs
            WHERE {where} AND scenario_id IS NOT NULL AND scenario_id != ''
            GROUP BY scenario_id
            ORDER BY runs DESC
        """
        try:
            with self._conn() as c:
                rows = c.execute(q, params).fetchall()
        except FileNotFoundError:
            return []
        return [dict(r) for r in rows]

    def compare_mutations(self, mutation_a: str, mutation_b: str) -> dict:
        """Side-by-side per (scenario, provider) for two mutations.

        Returns {a: {mutation, generation, runs_count}, b: …, cells:
        [{scenario, provider, a_runs, a_avg_dur, b_runs, b_avg_dur,
          a_completed, b_completed}]}.

        Useful for "did mutation B regress on REV-001/qwen3-6 vs A?"
        Pure aggregate — for full per-attempt data the caller drills
        via /api/search/runs?mutation=X.
        """
        try:
            with self._conn() as c:
                # Per-mutation summary
                summaries = {}
                for label, mut in (("a", mutation_a), ("b", mutation_b)):
                    row = c.execute(
                        """SELECT mutation, MIN(generation) AS generation,
                                  COUNT(*) AS runs_count
                           FROM runs WHERE mutation=?""", (mut,)
                    ).fetchone()
                    summaries[label] = dict(row) if row else {
                        "mutation": mut, "generation": None, "runs_count": 0
                    }

                # Per (scenario, provider) cells, joined.
                rows = c.execute(
                    """SELECT
                           COALESCE(a.scenario_id, b.scenario_id) AS scenario,
                           COALESCE(a.model, b.model)             AS provider,
                           a.runs        AS a_runs,
                           a.avg_dur_ms  AS a_avg_dur,
                           a.completed   AS a_completed,
                           b.runs        AS b_runs,
                           b.avg_dur_ms  AS b_avg_dur,
                           b.completed   AS b_completed
                       FROM
                         (SELECT scenario_id, model,
                                 COUNT(*) AS runs,
                                 AVG(duration_ms) AS avg_dur_ms,
                                 SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                          FROM runs WHERE mutation=?
                          GROUP BY scenario_id, model) a
                       FULL OUTER JOIN
                         (SELECT scenario_id, model,
                                 COUNT(*) AS runs,
                                 AVG(duration_ms) AS avg_dur_ms,
                                 SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                          FROM runs WHERE mutation=?
                          GROUP BY scenario_id, model) b
                       ON a.scenario_id=b.scenario_id AND a.model=b.model
                       ORDER BY scenario, provider""",
                    (mutation_a, mutation_b),
                ).fetchall()
        except sqlite3.OperationalError:
            # SQLite < 3.39 has no FULL OUTER JOIN — fall back to UNION.
            with self._conn() as c:
                summaries = {}
                for label, mut in (("a", mutation_a), ("b", mutation_b)):
                    row = c.execute(
                        """SELECT mutation, MIN(generation) AS generation,
                                  COUNT(*) AS runs_count
                           FROM runs WHERE mutation=?""", (mut,)
                    ).fetchone()
                    summaries[label] = dict(row) if row else {
                        "mutation": mut, "generation": None, "runs_count": 0
                    }
                cells: dict[tuple, dict] = {}
                for label, mut in (("a", mutation_a), ("b", mutation_b)):
                    rs = c.execute(
                        """SELECT scenario_id, model,
                                  COUNT(*) AS runs,
                                  AVG(duration_ms) AS avg_dur,
                                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                           FROM runs WHERE mutation=?
                           GROUP BY scenario_id, model""", (mut,)
                    ).fetchall()
                    for r in rs:
                        key = (r["scenario_id"] or "", r["model"] or "")
                        if key not in cells:
                            cells[key] = {"scenario": key[0], "provider": key[1]}
                        cells[key][f"{label}_runs"] = int(r["runs"] or 0)
                        cells[key][f"{label}_avg_dur"] = r["avg_dur"]
                        cells[key][f"{label}_completed"] = int(r["completed"] or 0)
                rows = list(cells.values())
                return {"a": summaries["a"], "b": summaries["b"], "cells": rows}
        return {"a": summaries["a"], "b": summaries["b"],
                "cells": [dict(r) for r in rows]}


    def mutation_scoring(self, mutation: str) -> dict:
        """Engineering-assessment-style scoring for one mutation.

        Walks every agent run with mutation=<m>, finds its linked
        judge run, parses the judge's agent_llm_response event for
        rich data (overall_score, required_comments, false_positives,
        agent_warnings, status_change_verdict, must_mention, …) and
        aggregates per-axis.

        Axes (mapping judge fields → engineering assessment dimensions):
          hard_skill   — correctness:  mean(overall_score adjusted for
                          recall + precision + status verdict).
          soft_skill   — communication: only computed for runs that
                          had `reply`-style assertions (interaction
                          scenarios); else null.
          methodology  — process quality: 1 - normalised agent_warnings
                          rate.
          status_ok_rate, found_rate, fp_rate — supporting metrics.

        by_cell: per (scenario, provider) cell of the same axes,
        used by the comparison view.
        by_warning_kind: histogram of agent_warning kinds.
        """
        out = {
            "mutation": mutation,
            "overall": {"runs": 0},
            "axes": {"hard_skill": None, "soft_skill": None, "methodology": None},
            "by_cell": [],
            "by_warning_kind": {},
        }
        if not mutation:
            return out
        try:
            with self._conn() as c:
                # All agent runs for this mutation, with their linked judge.
                pairs = c.execute("""
                    SELECT a.id AS agent_id, a.scenario_id, a.model,
                           a.linked_run_id AS judge_id
                    FROM runs a
                    WHERE a.kind='agent' AND a.mutation=?
                """, (mutation,)).fetchall()
                if not pairs:
                    return out

                # For each judge run, parse its agent_llm_response payload
                # to extract the rich verdict data.
                judge_data: dict[str, dict] = {}
                judge_ids = [p["judge_id"] for p in pairs if p["judge_id"]]
                if judge_ids:
                    placeholders = ",".join("?" for _ in judge_ids)
                    rows = c.execute(
                        f"""SELECT run_id, data_json FROM events
                            WHERE event_type='agent_llm_response'
                              AND run_id IN ({placeholders})""",
                        judge_ids,
                    ).fetchall()
                    for r in rows:
                        try:
                            d = json.loads(r["data_json"])
                            inner = d.get("data") or d.get("content") or {}
                            if isinstance(inner, str):
                                inner = json.loads(inner)
                            if isinstance(inner, dict):
                                judge_data[r["run_id"]] = inner
                        except Exception:
                            pass
        except Exception:
            return out

        # Aggregate
        cells: dict[tuple, list[dict]] = {}
        warning_hist: dict[str, int] = {}
        scores, found_rates, fp_counts, warnings_count = [], [], [], []
        status_ok = 0; status_total = 0
        soft_scores: list[float] = []
        for p in pairs:
            j = judge_data.get(p["judge_id"]) if p["judge_id"] else None
            if not j:
                continue
            score = float(j.get("overall_score") or 0)
            scores.append(score)

            req = j.get("required_comments") or []
            if req:
                found = sum(1 for r in req if r.get("found"))
                found_rates.append(found / max(1, len(req)))

            fps = j.get("false_positives") or []
            fp_counts.append(len(fps))

            warnings = j.get("agent_warnings") or []
            warnings_count.append(len(warnings))
            for w in warnings:
                kind = (w.get("kind") or "other") if isinstance(w, dict) else "other"
                warning_hist[kind] = warning_hist.get(kind, 0) + 1

            sv = j.get("status_change_verdict")
            if sv is not None and sv != "":
                status_total += 1
                if str(sv).lower() == "ok":
                    status_ok += 1

            # Soft-skill signal: if the judge ran reply-mode (must_mention
            # is present) compute a 0..1 score from it.
            mm = j.get("must_mention") or []
            ma_ok = j.get("must_address_satisfied")
            forb = j.get("forbidden_present") or []
            if mm:
                mm_match_rate = (
                    sum(1 for r in mm if r.get("matched")) / max(1, len(mm))
                )
                forb_violation = sum(1 for r in forb if not r.get("reasoning", "").lower().startswith("no"))
                forb_rate = forb_violation / max(1, len(forb)) if forb else 0
                soft = mm_match_rate * (1.0 if ma_ok else 0.5) * (1 - forb_rate)
                soft_scores.append(max(0.0, min(1.0, soft)))

            cell_key = (p["scenario_id"] or "", p["model"] or "")
            cells.setdefault(cell_key, []).append({
                "score": score,
                "found_rate": found_rates[-1] if req else None,
                "fp": len(fps),
                "warnings": len(warnings),
                "soft_score": soft_scores[-1] if (mm and soft_scores) else None,
            })

        n = len(scores)
        if n == 0:
            return out

        def _mean(xs):
            xs = [x for x in xs if x is not None]
            return (sum(xs) / len(xs)) if xs else None

        # Engineering-assessment axes.
        # hard_skill = mean(score) with mild penalty for false positives,
        # already implicit in the judge's score so we just use it.
        hard = _mean(scores)
        # methodology penalty: 1.0 - clamp(mean_warnings / 3, 0, 1).
        avg_warn = _mean(warnings_count)
        methodology = max(0.0, 1.0 - (avg_warn / 3.0)) if avg_warn is not None else None
        soft = _mean(soft_scores) if soft_scores else None

        out["overall"] = {
            "runs": n,
            "pass_rate": sum(1 for s in scores if s >= 0.7) / n,
            "mean_score": _mean(scores),
            "median_score": sorted(scores)[n // 2] if n else None,
            "found_rate": _mean(found_rates),
            "fp_per_run": _mean(fp_counts),
            "warnings_per_run": _mean(warnings_count),
            "status_ok_rate": (status_ok / status_total) if status_total else None,
        }
        out["axes"] = {
            "hard_skill": round(hard, 3) if hard is not None else None,
            "soft_skill": round(soft, 3) if soft is not None else None,
            "methodology": round(methodology, 3) if methodology is not None else None,
        }
        out["by_warning_kind"] = warning_hist
        out["by_cell"] = [
            {
                "scenario": k[0], "provider": k[1],
                "runs": len(v),
                "mean_score": _mean([x["score"] for x in v]),
                "found_rate": _mean([x["found_rate"] for x in v]),
                "fp_per_run": _mean([x["fp"] for x in v]),
                "warnings_per_run": _mean([x["warnings"] for x in v]),
            }
            for k, v in sorted(cells.items())
        ]
        return out

    def aggregate_by_mutation(self, f: RunFilter | None = None) -> list[dict]:
        """One row per mutation hash, UNION'd from two sources:

        1. `runs` — rich aggregate (runs count, durations, etc.)
           for mutations that have at least one run.
        2. `qa_planned_commits` — mutations that have been DISCOVERED
           and PLANNED by a schedule supervisor but may not have any
           runs yet. Surfaces them in the UI immediately so the
           operator sees "new commit detected" before any worker
           picks up the work.

        Match key is the short SHA (first 7 chars) since runs.mutation
        is short-formatted while qa_planned_commits.sha is full-length.
        """
        f = f or RunFilter(limit=10**9, offset=0)
        where, params = self._where_for_runs(f)
        try:
            with self._conn() as c:
                # Source A: rich aggregate from runs.
                rows = c.execute(f"""
                    SELECT mutation AS sha7,
                           MIN(generation)                            AS generation,
                           COUNT(*)                                   AS runs,
                           AVG(duration_ms)                           AS avg_duration_ms,
                           SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                           SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)     AS errored,
                           MIN(started_at)                            AS first_seen,
                           MAX(started_at)                            AS last_seen
                    FROM runs
                    WHERE {where} AND mutation IS NOT NULL AND mutation != ''
                    GROUP BY mutation
                """, params).fetchall()
                by_sha7 = {r["sha7"]: dict(r) for r in rows}

                # Source B: discovered-but-maybe-no-runs from
                # qa_planned_commits. Use SUBSTR to short-key match.
                # branches surfaces here so we can display
                # "discovered on master" before any run exists.
                planned = c.execute("""
                    SELECT SUBSTR(sha, 1, 7)                       AS sha7,
                           sha                                     AS full_sha,
                           GROUP_CONCAT(DISTINCT branch)           AS branches,
                           MIN(planned_at)                         AS first_planned,
                           MAX(planned_at)                         AS last_planned,
                           COUNT(DISTINCT plan_id)                 AS plans
                    FROM qa_planned_commits
                    GROUP BY SUBSTR(sha, 1, 7)
                """).fetchall()
        except FileNotFoundError:
            return []
        # Merge: planned-only rows get default/null run-derived fields.
        for p in planned:
            sha7 = p["sha7"]
            row = by_sha7.get(sha7)
            if row is None:
                # discovered but no runs yet
                row = {
                    "sha7": sha7,
                    "generation": None,
                    "runs": 0,
                    "avg_duration_ms": None,
                    "completed": 0, "errored": 0,
                    "first_seen": p["first_planned"],
                    "last_seen": p["last_planned"],
                }
                by_sha7[sha7] = row
            row["full_sha"] = p["full_sha"]
            row["branches"] = p["branches"]
            row["plans"] = p["plans"]
            row["first_planned"] = p["first_planned"]
            row["last_planned"] = p["last_planned"]

        # Output: rename sha7 → mutation for back-compat with the
        # existing UI which reads `mutation`.
        out = []
        for r in by_sha7.values():
            r["mutation"] = r.pop("sha7")
            out.append(r)
        out.sort(key=lambda r: (r.get("last_seen") or r.get("last_planned") or ""),
                 reverse=True)
        return out

    def aggregate_by_gene(self, f: RunFilter | None = None) -> list[dict]:
        """For each gene present in any run, count runs WITH and runs WITHOUT.

        Useful for §5e.12 evolution feedback: "does gene X help?".
        Pure count for now; pass-rate / score deltas come once the
        bench's qa_runs table is wired in (Phase 5).
        """
        f = f or RunFilter(limit=10**9, offset=0)
        where, params = self._where_for_runs(f)
        # Total in scope
        try:
            with self._conn() as c:
                total = c.execute(f"SELECT COUNT(*) FROM runs WHERE {where}",
                                  params).fetchone()[0]
                # Per-gene: rows that contain this gene
                rows = c.execute(f"""
                    SELECT json_each.value AS gene, COUNT(*) AS runs_with
                    FROM runs, json_each(runs.genes)
                    WHERE {where} AND runs.genes IS NOT NULL AND runs.genes != ''
                    GROUP BY json_each.value
                    ORDER BY runs_with DESC
                """, params).fetchall()
        except FileNotFoundError:
            return []
        out = []
        for r in rows:
            with_ = int(r["runs_with"])
            out.append({
                "gene": r["gene"],
                "runs_with": with_,
                "runs_without": int(total) - with_,
            })
        return out

    # ── Internals ───────────────────────────────────────────────────────

    _ALLOWED_SORT = {
        "started_at", "finished_at", "duration_ms", "model", "scenario_id",
        "agent_name", "kind", "total_tokens_paid", "findings_count",
    }

    def _safe_sort_col(self, name: str) -> str:
        if name not in self._ALLOWED_SORT:
            return "started_at"
        return name

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        d = dict(row)
        # Decode JSON arrays for ergonomic consumer access.
        for k in ("genes", "files_touched", "jira_keys", "scenario_tags"):
            v = d.get(k)
            if v and isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
        return d

    @staticmethod
    def _where_for_runs(f: RunFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        def eq(col: str, val):
            if val is not None and val != "":
                clauses.append(f"{col} = ?")
                params.append(val)

        eq("kind", f.kind)
        eq("agent_name", f.agent_name)
        eq("model", f.model)
        eq("status", f.status)
        eq("generation", f.generation)
        eq("project", f.project)
        eq("scenario_id", f.scenario_id)
        eq("linked_run_id", f.linked_run)

        if f.mutation:
            # Prefix match — short hashes are common in URLs.
            clauses.append("(mutation = ? OR mutation LIKE ?)")
            params.append(f.mutation)
            params.append(f.mutation + "%")
        if f.pr_url:
            clauses.append("pr_url = ?")
            params.append(f.pr_url)
        if f.since:
            clauses.append("started_at >= ?")
            params.append(f.since)
        if f.until:
            clauses.append("started_at <= ?")
            params.append(f.until)
        if f.duration_gt_ms is not None:
            clauses.append("duration_ms >= ?")
            params.append(int(f.duration_gt_ms))
        if f.tokens_gt is not None:
            clauses.append("total_tokens_paid >= ?")
            params.append(int(f.tokens_gt))

        # JSON-array contains: AND-semantic for genes / files / jira / tags
        for gene in f.genes:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(runs.genes) "
                "WHERE json_each.value = ?)"
            )
            params.append(gene)
        for gene in f.without_gene:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM json_each(runs.genes) "
                "WHERE json_each.value = ?)"
            )
            params.append(gene)
        if f.genes_any:
            placeholders = ",".join("?" for _ in f.genes_any)
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(runs.genes) "
                f"WHERE json_each.value IN ({placeholders}))"
            )
            params.extend(f.genes_any)
        if f.file:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(runs.files_touched) "
                "WHERE json_each.value = ?)"
            )
            params.append(f.file)
        if f.jira:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(runs.jira_keys) "
                "WHERE json_each.value = ?)"
            )
            params.append(f.jira)
        if f.scenario_tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(runs.scenario_tags) "
                "WHERE json_each.value = ?)"
            )
            params.append(f.scenario_tag)

        return " AND ".join(clauses), params
