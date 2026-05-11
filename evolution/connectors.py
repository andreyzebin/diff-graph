"""
Connectors to external systems. CLI-based implementations.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ConnectorConfig:
    tracing_db: str = ""            # path to traces.db (default: ~/.diffgraph/traces.db)
    analytics_cmd: str = ""          # e.g. "python /path/to/pr_analytics.py"
    analytics_db: str = ""           # path to pr-analytics DB
    benchmark_cmd: str = ""          # e.g. "python /path/to/benchmark/cli.py"
    benchmark_cwd: str = ""          # working dir for benchmark
    webhook_url: str = ""            # e.g. "http://localhost:8000"


class TracingConnector:
    """Query tracing subproject.

    The diff-graph-specific metrics (findings count, tokens paid,
    Mann-Whitney comparisons over those) used to live in
    `tracing.query` — that module was removed when the trace layer
    was made domain-agnostic. If evolution gets revived, rebuild
    these on top of: (1) the per-run JSON `--output` files agents
    write (canonical findings), (2) the generic `runs` /
    `otel_spans` tables (run metadata, timings, model)."""

    def __init__(self, db_path: str = ""):
        from orchestra.trace_db import DEFAULT_DB_PATH
        self.db_path = db_path or str(DEFAULT_DB_PATH)

    def metrics(self, prompt_hash: str) -> dict:
        raise NotImplementedError(
            "tracing.query.get_metrics was removed when the trace "
            "layer dropped findings_count/total_tokens_paid; rebuild "
            "on top of agent output JSONs"
        )

    def runs(self, prompt_hash: str, limit: int = 50) -> list[dict]:
        raise NotImplementedError(
            "tracing.query.get_runs was removed; use trace_db.TraceDBReader"
        )

    def compare(self, hash_a: str, hash_b: str) -> dict:
        raise NotImplementedError(
            "tracing.query.compare did Mann-Whitney on findings_count, "
            "which is no longer in the runs table"
        )


class AnalyticsConnector:
    """Query pr-analytics via CLI or direct DB."""

    def __init__(self, cmd: str = "", db_path: str = ""):
        self.cmd = cmd
        self.db_path = db_path

    def acceptance(self, prompt_hash: str) -> dict:
        if self.cmd:
            return self._run_cmd(f"acceptance --dg-hash {prompt_hash} --format json")
        if self.db_path:
            return self._query_db(prompt_hash)
        return {}

    def _run_cmd(self, args: str) -> dict:
        try:
            result = subprocess.run(
                f"{self.cmd} {args}",
                shell=True, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception as exc:
            log.warning("analytics cmd failed: %s", exc)
        return {}

    def _query_db(self, prompt_hash: str) -> dict:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN a.verdict = 'yes' THEN 1 END) as accepted,
                COUNT(CASE WHEN a.verdict = 'no' THEN 1 END) as rejected
            FROM pr_comments c
            LEFT JOIN comment_analysis a ON a.comment_id = c.id
            WHERE c.dg_hash = ? AND c.parent_id IS NULL
        """, (prompt_hash,)).fetchone()
        conn.close()
        total = row["total"] or 0
        accepted = row["accepted"] or 0
        rejected = row["rejected"] or 0
        return {
            "total_comments": total,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": round(accepted / (accepted + rejected), 3) if (accepted + rejected) > 0 else None,
        }


class BenchmarkConnector:
    """Run benchmarks via CLI."""

    def __init__(self, cmd: str = "", cwd: str = ""):
        self.cmd = cmd
        self.cwd = cwd

    def run(self, prompts_uri: str = "", scenario: str = "") -> dict:
        args = "run"
        if prompts_uri:
            args += f' --prompts="{prompts_uri}"'
        if scenario:
            args += f" --scenario {scenario}"
        args += " --format json 2>/dev/null || true"
        return self._run_cmd(args)

    def _run_cmd(self, args: str) -> dict:
        if not self.cmd:
            return {"error": "benchmark cmd not configured"}
        try:
            result = subprocess.run(
                f"{self.cmd} {args}",
                shell=True, capture_output=True, text=True,
                timeout=600, cwd=self.cwd or None,
            )
            # Try to parse last JSON line from output
            for line in reversed(result.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
        except Exception as exc:
            log.warning("benchmark cmd failed: %s", exc)
        return {}


class WebhookConnector:
    """Manage webhook routes via API."""

    def __init__(self, url: str = "http://localhost:8000"):
        self.url = url.rstrip("/")

    def list_routes(self) -> list[dict]:
        return self._get("/routes").get("routes", [])

    def add_route(self, name: str, when: str, agent: str, sample: int = 5, **kwargs) -> dict:
        return self._post("/api/routes", {"name": name, "when": when, "agent": agent, "sample": sample, **kwargs})

    def update_sample(self, name: str, sample: int) -> dict:
        return self._patch(f"/api/routes/{name}", {"sample": sample})

    def remove_route(self, name: str) -> dict:
        return self._delete(f"/api/routes/{name}")

    def reload(self) -> dict:
        return self._post("/api/reload", {})

    def _get(self, path: str) -> dict:
        return self._http("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._http("POST", path, body)

    def _patch(self, path: str, body: dict) -> dict:
        return self._http("PATCH", path, body)

    def _delete(self, path: str) -> dict:
        return self._http("DELETE", path)

    def _http(self, method: str, path: str, body: dict = None) -> dict:
        import urllib.request
        url = self.url + path
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            log.warning("webhook %s %s failed: %s", method, path, exc)
            return {"error": str(exc)}
