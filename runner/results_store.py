from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from runner.scorer import ScenarioResult


class ResultsStore:
    def __init__(self, store_path: Path, db_path: Path):
        self._runs_dir = store_path / "runs"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    run_at TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    scenario_name TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    score REAL NOT NULL,
                    required_found INTEGER,
                    required_total INTEGER,
                    false_positives INTEGER,
                    duration_seconds REAL,
                    agent_url TEXT,
                    tags TEXT
                )
            """)
            conn.commit()

    def save_run(
        self,
        run_id: str,
        results: list[ScenarioResult],
        agent_url: str = "",
        tags: list[str] | None = None,
    ) -> Path:
        run_data = {
            "run_id": run_id,
            "run_at": datetime.utcnow().isoformat(),
            "agent_url": agent_url,
            "results": [_result_to_dict(r) for r in results],
        }
        json_path = self._runs_dir / f"{run_id}.json"
        json_path.write_text(json.dumps(run_data, indent=2, default=str))

        with sqlite3.connect(self._db_path) as conn:
            for r in results:
                conn.execute(
                    "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{run_id}/{r.scenario_id}",
                        r.run_at.isoformat(),
                        r.scenario_id,
                        r.scenario_name,
                        r.verdict,
                        r.score,
                        r.required_found,
                        r.required_total,
                        r.false_positives,
                        r.duration_seconds,
                        agent_url,
                        json.dumps(tags or []),
                    ),
                )
            conn.commit()

        return json_path

    def get_last_run(self) -> list[ScenarioResult] | None:
        runs = sorted(self._runs_dir.glob("*.json"), reverse=True)
        if not runs:
            return None
        data = json.loads(runs[0].read_text())
        return [_dict_to_result(r) for r in data["results"]]

    def get_run_by_id(self, run_id: str) -> list[ScenarioResult] | None:
        json_path = self._runs_dir / f"{run_id}.json"
        if not json_path.exists():
            return None
        data = json.loads(json_path.read_text())
        return [_dict_to_result(r) for r in data["results"]]

    def list_runs(self, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT run_id, run_at, agent_url,
                       COUNT(*) as total,
                       SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) as passed,
                       AVG(score) as avg_score
                FROM (
                    SELECT substr(run_id, 1,
                        CASE WHEN instr(run_id, '/') > 0
                             THEN instr(run_id, '/') - 1
                             ELSE length(run_id) END
                    ) as run_id,
                    run_at, agent_url, verdict, score
                    FROM runs
                )
                GROUP BY run_id
                ORDER BY run_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": r[0],
                "run_at": r[1],
                "agent_url": r[2],
                "total": r[3],
                "passed": r[4],
                "avg_score": round(r[5], 3),
            }
            for r in rows
        ]


def _result_to_dict(r: ScenarioResult) -> dict:
    return {
        "scenario_id": r.scenario_id,
        "scenario_name": r.scenario_name,
        "verdict": r.verdict,
        "score": r.score,
        "required_found": r.required_found,
        "required_total": r.required_total,
        "false_positives": r.false_positives,
        "location_accuracy": r.location_accuracy,
        "status_change_verdict": r.status_change_verdict,
        "inline_ratio": r.inline_ratio,
        "total_comments": r.total_comments,
        "duration_seconds": r.duration_seconds,
        "judge_summary": r.judge_summary,
        "run_at": r.run_at.isoformat() if r.run_at else None,
        "error": r.error,
        "pr_url": r.pr_url,
    }


def _dict_to_result(d: dict) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=d["scenario_id"],
        scenario_name=d["scenario_name"],
        verdict=d["verdict"],
        score=d["score"],
        required_found=d.get("required_found", 0),
        required_total=d.get("required_total", 0),
        false_positives=d.get("false_positives", 0),
        location_accuracy=d.get("location_accuracy", 0.0),
        status_change_verdict=d.get("status_change_verdict", "unknown"),
        inline_ratio=d.get("inline_ratio", 0.0),
        total_comments=d.get("total_comments", 0),
        duration_seconds=d.get("duration_seconds", 0.0),
        judge_summary=d.get("judge_summary", ""),
        run_at=datetime.fromisoformat(d["run_at"]) if d.get("run_at") else datetime.utcnow(),
        error=d.get("error"),
        pr_url=d.get("pr_url"),
    )
