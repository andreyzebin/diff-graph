"""
Tests for tracing query functions.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tracing.query import get_metrics, get_runs, compare


@pytest.fixture
def test_db():
    """Create a temp trace DB with sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.executescript("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            model TEXT,
            pr_url TEXT,
            diff_summary TEXT,
            total_tokens_paid INTEGER,
            findings_count INTEGER,
            status TEXT DEFAULT 'completed',
            prompt_source TEXT,
            prompt_hash TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            agent_id TEXT,
            agent_name TEXT,
            timestamp TEXT,
            event_type TEXT,
            step INTEGER,
            data_json TEXT
        );
    """)

    # Insert sample runs
    for i in range(5):
        conn.execute(
            "INSERT INTO runs (id, started_at, model, total_tokens_paid, findings_count, status, prompt_hash) "
            "VALUES (?, ?, ?, ?, ?, 'completed', ?)",
            (f"run-a-{i}", f"2026-04-{10+i}T10:00:00", "deepseek", 5000 + i * 100, 2 + (i % 2), "hash-a"),
        )
    for i in range(3):
        conn.execute(
            "INSERT INTO runs (id, started_at, model, total_tokens_paid, findings_count, status, prompt_hash) "
            "VALUES (?, ?, ?, ?, ?, 'completed', ?)",
            (f"run-b-{i}", f"2026-04-{10+i}T10:00:00", "deepseek", 8000 + i * 200, 1, "hash-b"),
        )

    # Insert sample events for cache ratio
    for i in range(5):
        conn.execute(
            "INSERT INTO events (run_id, agent_id, agent_name, event_type, step, data_json) "
            "VALUES (?, 'a1', 'lead', 'agent_llm_response', ?, ?)",
            (f"run-a-{i}", 0, json.dumps({"usage": {"prompt_tokens": 1000, "cached_tokens": 400, "completion_tokens": 50}})),
        )

    conn.commit()
    conn.close()
    yield tmp.name
    Path(tmp.name).unlink(missing_ok=True)


class TestGetMetrics:

    def test_basic(self, test_db):
        m = get_metrics("hash-a", db_path=test_db)
        assert m.runs_count == 5
        assert m.prompt_hash == "hash-a"
        assert m.findings_avg > 0
        assert m.tokens_per_finding > 0
        assert m.total_tokens_avg > 0

    def test_cache_ratio(self, test_db):
        m = get_metrics("hash-a", db_path=test_db)
        assert m.cache_ratio > 0  # 400/1000 = 0.4

    def test_unknown_hash(self, test_db):
        m = get_metrics("nonexistent", db_path=test_db)
        assert m.runs_count == 0
        assert m.findings_avg == 0

    def test_to_dict(self, test_db):
        m = get_metrics("hash-a", db_path=test_db)
        d = m.to_dict()
        assert "prompt_hash" in d
        assert "runs_count" in d
        assert isinstance(d["findings_avg"], float)


class TestGetRuns:

    def test_list(self, test_db):
        runs = get_runs("hash-a", db_path=test_db)
        assert len(runs) == 5
        assert all(r["prompt_hash"] == "hash-a" for r in runs)

    def test_limit(self, test_db):
        runs = get_runs("hash-a", db_path=test_db, limit=2)
        assert len(runs) == 2

    def test_empty(self, test_db):
        runs = get_runs("nonexistent", db_path=test_db)
        assert runs == []


class TestCompare:

    def test_compare(self, test_db):
        c = compare("hash-a", "hash-b", db_path=test_db)
        assert c.a.runs_count == 5
        assert c.b.runs_count == 3
        assert "findings_avg" in c.delta
        assert "tokens_per_finding" in c.delta
        for metric, d in c.delta.items():
            assert "a" in d and "b" in d and "diff" in d

    def test_compare_to_dict(self, test_db):
        c = compare("hash-a", "hash-b", db_path=test_db)
        d = c.to_dict()
        assert "a" in d and "b" in d and "delta" in d
