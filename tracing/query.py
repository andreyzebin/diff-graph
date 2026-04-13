"""
Trace DB query functions — aggregate metrics, compare generations.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestra.trace_db import DEFAULT_DB_PATH


@dataclass
class Metrics:
    prompt_hash: str
    runs_count: int
    findings_avg: float
    tokens_per_finding: float
    total_tokens_avg: float
    steps_avg: float
    cache_ratio: float

    def to_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v
                for k, v in self.__dict__.items()}


@dataclass
class Comparison:
    a: Metrics
    b: Metrics
    delta: dict  # metric_name → {a, b, diff, diff_pct}
    p_value: float | None  # Mann-Whitney U test if scipy available

    def to_dict(self) -> dict:
        return {
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
            "delta": self.delta,
            "p_value": self.p_value,
        }


def _resolve_hash(conn: sqlite3.Connection, prefix: str) -> str:
    """Resolve hash prefix to full hash. Returns prefix if no match or ambiguous."""
    if len(prefix) >= 32:
        return prefix
    rows = conn.execute(
        "SELECT DISTINCT prompt_hash FROM runs WHERE prompt_hash LIKE ? LIMIT 2",
        (prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return prefix


def get_metrics(
    prompt_hash: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    since: str | None = None,
) -> Metrics:
    """Aggregate metrics for runs with given prompt_hash (prefix match)."""
    conn = _connect(db_path)
    prompt_hash = _resolve_hash(conn, prompt_hash)
    where = "WHERE prompt_hash = ?"
    params: list = [prompt_hash]
    if since:
        where += " AND started_at >= ?"
        params.append(since)

    row = conn.execute(f"""
        SELECT
            COUNT(*) as cnt,
            AVG(findings_count) as findings_avg,
            AVG(total_tokens_paid) as tokens_avg,
            SUM(total_tokens_paid) as tokens_sum,
            SUM(findings_count) as findings_sum
        FROM runs
        {where} AND status = 'completed'
    """, params).fetchone()

    cnt = row["cnt"] or 0
    findings_avg = row["findings_avg"] or 0
    tokens_avg = row["tokens_avg"] or 0
    findings_sum = row["findings_sum"] or 0
    tokens_per_finding = (row["tokens_sum"] / findings_sum) if findings_sum > 0 else 0

    # Steps + cache from events
    steps_avg = 0.0
    cache_ratio = 0.0
    if cnt > 0:
        run_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM runs {where} AND status = 'completed'", params
        ).fetchall()]
        if run_ids:
            placeholders = ",".join("?" * len(run_ids))
            ev_row = conn.execute(f"""
                SELECT
                    COUNT(DISTINCT run_id || '-' || agent_id || '-' || step) as total_steps,
                    COUNT(DISTINCT run_id) as run_count
                FROM events
                WHERE run_id IN ({placeholders})
                AND event_type = 'agent_llm_response'
            """, run_ids).fetchone()
            if ev_row["run_count"]:
                steps_avg = ev_row["total_steps"] / ev_row["run_count"]

            cache_row = conn.execute(f"""
                SELECT
                    SUM(json_extract(data_json, '$.usage.cached_tokens')) as cached,
                    SUM(json_extract(data_json, '$.usage.prompt_tokens')) as total
                FROM events
                WHERE run_id IN ({placeholders})
                AND event_type = 'agent_llm_response'
            """, run_ids).fetchone()
            if cache_row["total"] and cache_row["total"] > 0:
                cache_ratio = (cache_row["cached"] or 0) / cache_row["total"]

    conn.close()
    return Metrics(
        prompt_hash=prompt_hash,
        runs_count=cnt,
        findings_avg=findings_avg,
        tokens_per_finding=tokens_per_finding,
        total_tokens_avg=tokens_avg,
        steps_avg=steps_avg,
        cache_ratio=cache_ratio,
    )


def get_runs(
    prompt_hash: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 50,
) -> list[dict]:
    """List runs for a prompt hash (prefix match)."""
    conn = _connect(db_path)
    prompt_hash = _resolve_hash(conn, prompt_hash)
    rows = conn.execute("""
        SELECT id, started_at, finished_at, model, findings_count,
               total_tokens_paid, status, prompt_source, prompt_hash
        FROM runs WHERE prompt_hash = ?
        ORDER BY started_at DESC LIMIT ?
    """, (prompt_hash, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compare(
    hash_a: str,
    hash_b: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> Comparison:
    """Compare two prompt generations (prefix match)."""
    conn = _connect(db_path)
    hash_a = _resolve_hash(conn, hash_a)
    hash_b = _resolve_hash(conn, hash_b)
    conn.close()
    a = get_metrics(hash_a, db_path)
    b = get_metrics(hash_b, db_path)

    delta = {}
    for field in ["findings_avg", "tokens_per_finding", "total_tokens_avg", "steps_avg", "cache_ratio"]:
        va = getattr(a, field)
        vb = getattr(b, field)
        diff = vb - va
        diff_pct = (diff / va * 100) if va != 0 else 0
        delta[field] = {"a": round(va, 3), "b": round(vb, 3), "diff": round(diff, 3), "diff_pct": round(diff_pct, 1)}

    # Statistical test on tokens_per_finding
    p_value = _mann_whitney(hash_a, hash_b, db_path)

    return Comparison(a=a, b=b, delta=delta, p_value=p_value)


def _mann_whitney(hash_a: str, hash_b: str, db_path) -> float | None:
    """Mann-Whitney U test on findings_count distributions."""
    try:
        from scipy.stats import mannwhitneyu
    except ImportError:
        return None

    conn = _connect(db_path)
    a_vals = [r["findings_count"] for r in conn.execute(
        "SELECT findings_count FROM runs WHERE prompt_hash=? AND status='completed' AND findings_count IS NOT NULL",
        (hash_a,)).fetchall()]
    b_vals = [r["findings_count"] for r in conn.execute(
        "SELECT findings_count FROM runs WHERE prompt_hash=? AND status='completed' AND findings_count IS NOT NULL",
        (hash_b,)).fetchall()]
    conn.close()

    if len(a_vals) < 3 or len(b_vals) < 3:
        return None
    try:
        _, p = mannwhitneyu(a_vals, b_vals, alternative="two-sided")
        return round(p, 4)
    except Exception:
        return None


def tag_run(
    run_id: str,
    tag: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Add a tag to a run. Tags stored as comma-separated string."""
    conn = _connect(db_path)
    row = conn.execute("SELECT tags FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        conn.close()
        return
    existing = set(filter(None, (row["tags"] or "").split(",")))
    existing.add(tag)
    conn.execute("UPDATE runs SET tags = ? WHERE id = ?", (",".join(sorted(existing)), run_id))
    conn.commit()
    conn.close()


def untag_run(
    run_id: str,
    tag: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """Remove a tag from a run."""
    conn = _connect(db_path)
    row = conn.execute("SELECT tags FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        conn.close()
        return
    existing = set(filter(None, (row["tags"] or "").split(",")))
    existing.discard(tag)
    conn.execute("UPDATE runs SET tags = ? WHERE id = ?", (",".join(sorted(existing)) or None, run_id))
    conn.commit()
    conn.close()


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn
