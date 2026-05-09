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

    def aggregate_by_mutation(self, f: RunFilter | None = None) -> list[dict]:
        """One row per mutation hash — runs count, duration percentiles,
        link to generation. Substrate for "is mutation B better than A"
        comparisons (§5e.11)."""
        f = f or RunFilter(limit=10**9, offset=0)
        where, params = self._where_for_runs(f)
        q = f"""
            SELECT mutation,
                   MIN(generation)                         AS generation,
                   COUNT(*)                                AS runs,
                   AVG(duration_ms)                        AS avg_duration_ms,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)     AS errored,
                   MIN(started_at)                         AS first_seen,
                   MAX(started_at)                         AS last_seen
            FROM runs
            WHERE {where} AND mutation IS NOT NULL AND mutation != ''
            GROUP BY mutation
            ORDER BY last_seen DESC
        """
        try:
            with self._conn() as c:
                rows = c.execute(q, params).fetchall()
        except FileNotFoundError:
            return []
        return [dict(r) for r in rows]

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
