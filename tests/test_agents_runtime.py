"""Tests for the agents-runtime provider (orchestra/agents_runtime.py).

Phase 1 scope: the AgentsRuntime interface + FakeAgentsRuntime +
TraceDBObserver + InProcessRuntime wrapper. The spawn hot-path
rewiring (builtin.py agent_spawn → runtime) lands in Phase 2.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestra.agents_runtime import (
    AgentDescriptor,
    AgentsRuntime,
    FakeAgentsRuntime,
    InProcessRuntime,
    RunResult,
    RunStatus,
    SpawnSpec,
    TokenAccounting,
    TraceDBObserver,
)
from orchestra.trace_db import ensure_trace_schema


# ── FakeAgentsRuntime ─────────────────────────────────────────────────────


FIXTURE = {
    "agents": [
        {"name": "investigator", "summary": "deep cross-repo dive",
         "tools": ["diff_read_file", "jira_dev_info"]},
        {"name": "worker", "summary": "single focused task"},
    ],
    "spawns": [
        {"match": {"agent_name": "investigator", "focus_contains": "cheapest"},
         "result": {"output": "found the cheapest-item bug",
                    "sgr_summary": "confidence=high", "steps": 7, "tokens": 4200}},
        {"match": {"agent_name": "investigator"},
         "result": {"output": "generic investigator answer",
                    "steps": 3, "tokens": 900}},
    ],
    "runs": {
        "fakerun-001": {"state": "completed", "agent_name": "investigator",
                        "steps": 7,
                        "tokens": {"tokens_paid": 4200, "tokens_in": 3800,
                                   "tokens_out": 400, "tokens_cached": 1000},
                        "trace": {"agent_id": "fake", "steps": 7}},
    },
}


def test_fake_runtime_is_protocol_conformant() -> None:
    assert isinstance(FakeAgentsRuntime(FIXTURE), AgentsRuntime)


def test_fake_spawn_focus_match() -> None:
    rt = FakeAgentsRuntime(FIXTURE)
    h = rt.spawn(SpawnSpec(agent_name="investigator",
                           focus="track down the cheapest item logic"))
    r = rt.await_result(h)
    assert r.status == "completed"
    assert r.output == "found the cheapest-item bug"
    assert r.steps == 7 and r.tokens == 4200
    assert r.sgr_summary == "confidence=high"


def test_fake_spawn_catch_all() -> None:
    """A spawns entry with no focus_contains is the catch-all — it
    matches any focus for that agent_name."""
    rt = FakeAgentsRuntime(FIXTURE)
    h = rt.spawn(SpawnSpec(agent_name="investigator", focus="anything else"))
    assert rt.await_result(h).output == "generic investigator answer"


def test_fake_spawn_unmatched_fails_loud() -> None:
    """An unmatched spawn returns a failed RunResult naming the
    unmatched (agent, focus) — tests fail loud, not silently."""
    rt = FakeAgentsRuntime(FIXTURE)
    h = rt.spawn(SpawnSpec(agent_name="ghost", focus="x"))
    r = rt.await_result(h)
    assert r.status == "failed"
    assert "ghost" in r.error


def test_fake_observation() -> None:
    rt = FakeAgentsRuntime(FIXTURE)
    st = rt.status("fakerun-001")
    assert st.state == "completed"
    assert st.steps == 7
    assert st.tokens.tokens_paid == 4200
    assert st.tokens.tokens_in == 3800
    assert rt.tokens("fakerun-001").tokens_paid == 4200
    assert rt.trace("fakerun-001") == {"agent_id": "fake", "steps": 7}
    # Unknown run — graceful.
    assert rt.status("no-such-run").state == "unknown"


def test_fake_observe() -> None:
    """observe() replays the fixture's per-run events list."""
    fx = {"runs": {"r1": {"events": [
        {"event_type": "agent_started", "step": 0},
        {"event_type": "agent_tool_request", "step": 1, "data": {"tool": "probe"}},
    ]}}}
    rt = FakeAgentsRuntime(fx)
    evs = list(rt.observe("r1"))
    assert [e["event_type"] for e in evs] == ["agent_started", "agent_tool_request"]
    assert list(rt.observe("missing")) == []


def test_spawn_spec_async_defaults() -> None:
    """SpawnSpec carries TODO §13 sched/callback, defaulting to the
    current synchronous behaviour."""
    s = SpawnSpec(agent_name="investigator", focus="x")
    assert s.sched == "sync"
    assert s.callback is True
    a = SpawnSpec(agent_name="investigator", sched="async", callback=False)
    assert a.sched == "async" and a.callback is False


def test_fake_list_agents() -> None:
    rt = FakeAgentsRuntime(FIXTURE)
    agents = rt.list_agents()
    assert {a.name for a in agents} == {"investigator", "worker"}
    inv = next(a for a in agents if a.name == "investigator")
    assert inv.summary == "deep cross-repo dive"
    assert "jira_dev_info" in inv.tools


def test_fake_from_yaml(tmp_path: Path) -> None:
    import yaml
    p = tmp_path / "runtime-fixture.yaml"
    p.write_text(yaml.safe_dump(FIXTURE), encoding="utf-8")
    rt = FakeAgentsRuntime.from_yaml(p)
    assert rt.list_agents()[0].name == "investigator"


# ── TraceDBObserver ───────────────────────────────────────────────────────


def _seed_run(db: Path, run_id: str) -> None:
    """Write one completed run with two events into a fresh trace DB."""
    ensure_trace_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO runs (id, started_at, finished_at, status, agent_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, "2026-05-20T10:00:00", "2026-05-20T10:01:00",
         "completed", "investigator"),
    )
    # agent_started so get_run_trace registers the root agent
    conn.execute(
        "INSERT INTO events (run_id, agent_id, agent_name, timestamp, "
        "event_type, step, data_json) VALUES (?,?,?,?,?,?,?)",
        (run_id, "a1", "investigator", "2026-05-20T10:00:00",
         "agent_started", 0, '{"agent_id": "a1"}'),
    )
    conn.commit()
    conn.close()


def test_observer_status_of_completed_run(tmp_path: Path) -> None:
    db = tmp_path / "traces.db"
    _seed_run(db, "run-xyz")
    obs = TraceDBObserver(db)
    st = obs.status("run-xyz")
    assert st.state == "completed"
    assert st.agent_name == "investigator"
    assert st.run_id == "run-xyz"


def test_observer_unknown_run(tmp_path: Path) -> None:
    db = tmp_path / "traces.db"
    _seed_run(db, "run-xyz")
    obs = TraceDBObserver(db)
    assert obs.status("missing").state == "unknown"


def test_observer_observe_replays_events(tmp_path: Path) -> None:
    db = tmp_path / "traces.db"
    _seed_run(db, "run-xyz")
    obs = TraceDBObserver(db)
    evs = list(obs.observe("run-xyz"))
    assert len(evs) == 1
    assert evs[0]["event_type"] == "agent_started"
    assert evs[0]["data"] == {"agent_id": "a1"}


def test_observer_missing_db_is_graceful(tmp_path: Path) -> None:
    """No DB file at all — observation degrades to 'unknown', never
    raises (callers are tools / the budget layer / the judge — none
    should crash because a trace store isn't there yet)."""
    obs = TraceDBObserver(tmp_path / "does-not-exist.db")
    assert obs.status("any").state == "unknown"
    assert obs.trace("any") == {}
    assert obs.tokens("any").tokens_paid == 0


# ── InProcessRuntime ──────────────────────────────────────────────────────


def test_inprocess_spawn_delegates_to_callback() -> None:
    """InProcessRuntime.spawn() runs the injected spawn_fn and caches
    its RunResult for a free await_result()."""
    calls: list[SpawnSpec] = []

    def fake_spawn_fn(spec: SpawnSpec) -> RunResult:
        calls.append(spec)
        return RunResult(agent_id="child-1", agent_name=spec.agent_name,
                         status="completed", output="done", steps=5,
                         tokens=1234, run_id="run-1")

    rt = InProcessRuntime(
        fake_spawn_fn,
        list_agents_fn=lambda: [AgentDescriptor(name="investigator")],
    )
    handle = rt.spawn(SpawnSpec(agent_name="investigator", focus="f"))
    assert handle.agent_id == "child-1"
    assert handle.run_id == "run-1"
    assert len(calls) == 1 and calls[0].focus == "f"

    result = rt.await_result(handle)
    assert result.output == "done" and result.steps == 5

    assert rt.list_agents()[0].name == "investigator"


def test_inprocess_is_protocol_conformant() -> None:
    rt = InProcessRuntime(
        lambda spec: RunResult(agent_id="x", agent_name="y"),
        list_agents_fn=lambda: [],
    )
    assert isinstance(rt, AgentsRuntime)
