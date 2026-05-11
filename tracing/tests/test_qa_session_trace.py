"""
Drill-in page /qa/sessions/{run_id} + supporting APIs.

Pins:
  1. The HTML route serves a page that wires in `window.RUN_ID`
     and the sessionTraceView Alpine component (so the SPA can hydrate).
  2. /api/runs/{id} returns metadata for the header strip — and 404s
     cleanly for unknown ids.
  3. /api/runs/{id}/json returns the *prepared* tree shape
     (paired_steps, conf_trail, totals) — the same shape the old
     Jinja /runs/{id}/trace renderer used via _prepare_agent.
  4. Legacy URLs /qa/traces and /qa/runs/{id} redirect (308) to the
     new canonical names with query string preserved.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def db_with_run(monkeypatch):
    """Stub SQLiteTraceStore so `/api/runs/{id}` doesn't have to
    open the real ~/.diffgraph/traces.db. The endpoint calls
    `SQLiteTraceStore().list_runs(filter)` — we replace that with
    a tiny stand-in keyed on session_id.

    Why a stub instead of a tmp DB: the real `SQLiteTraceStore`
    captures `DEFAULT_DB_PATH` as a default-argument value at import
    time (Python evaluates defaults once), so monkeypatching the
    module attribute doesn't redirect new instances. The stub
    sidesteps that entirely and keeps the test focused on the
    endpoint's shape contract, not on DB plumbing."""
    canned = {
        "testrun01abcd": {
            "id": "testrun01abcd",
            "agent_name": "investigator",
            "model": "deepseek-chat",
            "status": "completed",
            "started_at": "2026-05-11T10:00:00+00:00",
            "scenario_id": "INV-U-001-cancel-npe",
            "plan_id": 107,
            "duration_ms": 35000,
        },
    }

    class _StubStore:
        def list_runs(self, f):
            sid = getattr(f, "session_id", None)
            return [canned[sid]] if sid in canned else []

    import tracing.server.store as store_mod
    monkeypatch.setattr(store_mod, "SQLiteTraceStore",
                        lambda *a, **kw: _StubStore())
    return canned


@pytest.fixture
def client():
    from tracing.server.app import app
    return TestClient(app)


class TestSessionTraceHtmlRoute:
    def test_serves_html_with_run_id_wired(self, client):
        """The route returns 200 and the page wires window.RUN_ID and
        the Alpine entry point. Doesn't need a real run in the DB —
        the page hydrates client-side from /api/runs/{id}."""
        r = client.get("/qa/sessions/sample-id-1234?from=plan%3D107")
        assert r.status_code == 200
        body = r.text
        assert 'window.RUN_ID = "sample-id-1234"' in body
        assert 'sessionTraceView()' in body, "Alpine component not wired"
        assert "_qa_nav" in body or "qa-nav" in body, "QA nav include missing"
        # The "back to filtered list" template should be present so
        # the client can render it once `from=` is parsed.
        assert "back to filtered list" in body


class TestLegacyRedirects:
    def test_old_traces_redirects(self, client):
        """Old /qa/traces tabs / bookmarks land on /qa/sessions."""
        r = client.get("/qa/traces?plan=107&scenario=X",
                       follow_redirects=False)
        assert r.status_code == 308
        loc = r.headers["location"]
        assert loc.endswith("/qa/sessions?plan=107&scenario=X"), loc

    def test_old_run_url_redirects(self, client):
        """Old /qa/runs/{id} tabs land on /qa/sessions/{id}, query
        string preserved."""
        r = client.get("/qa/runs/abc?from=plan%3D107",
                       follow_redirects=False)
        assert r.status_code == 308
        loc = r.headers["location"]
        assert loc.endswith("/qa/sessions/abc?from=plan%3D107"), loc


class TestRunMetaApi:
    def test_returns_header_strip_fields(self, client, db_with_run):
        r = client.get("/api/runs/testrun01abcd")
        assert r.status_code == 200
        body = r.json()
        assert body.get("data"), f"expected data payload, got {body}"
        d = body["data"]
        # These are the fields the header strip needs.
        for k in ("id", "agent_name", "model", "status", "started_at"):
            assert k in d, f"missing field: {k} (got keys: {sorted(d)})"
        assert d["id"] == "testrun01abcd"
        assert d["status"] == "completed"

    def test_404_for_unknown(self, client, db_with_run):
        r = client.get("/api/runs/does-not-exist")
        assert r.status_code == 404
        assert r.json().get("data") is None


class TestRunJsonApiShape:
    def test_returns_prepared_tree(self, client, monkeypatch):
        """/api/runs/{id}/json must return the *prepared* tree shape
        (paired_steps, totals) — guards against accidentally
        regressing it back to the raw get_run_trace output, which
        would force the SPA to do pairing client-side. The endpoint
        wraps get_run_trace with orchestra.trace._prepare_agent; we
        stub the reader so the test doesn't depend on a real DB."""
        class _StubReader:
            def get_run_trace(self, run_id):
                # Minimal raw shape that _prepare_agent accepts.
                return {
                    "agent_id": "ag1",
                    "agent_name": "investigator",
                    "steps": 0,
                    "tokens_paid": 0,
                    "sgr": [],
                    "llm_calls": [],
                    "children": [],
                    "output": None,
                }
            def close(self):
                pass

        import tracing.server.app as app_mod
        monkeypatch.setattr(app_mod, "_get_reader", lambda: _StubReader())

        r = client.get("/api/runs/ag1/json")
        assert r.status_code == 200
        tree = r.json()
        # Top-level fields per orchestra.trace._prepare_agent contract.
        for k in ("agent_id", "agent_name", "paired_steps",
                  "children", "total_in", "total_out", "tokens_paid",
                  "conf_trail"):
            assert k in tree, f"missing prepared field: {k}"
        # paired_steps is a list (empty for a minimal run is fine,
        # but it MUST exist as a list, not None or absent).
        assert isinstance(tree["paired_steps"], list)
        assert isinstance(tree["children"], list)
