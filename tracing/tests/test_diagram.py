"""
Scope-agnostic diagram builder.

The builder has three independent moving parts; the tests pin each
in isolation plus one integration through `build_diagram`:

  1. resolve_runs(scope_uri) — URI → transitive closure of run_ids.
     We pin `session://` (own run + linked partner) and
     `scenario_run://` (agent + judge by scenario_run_id).
  2. events_from_runs(...) — canonical Event list with stable kinds
     and tz-aware timestamps so events from different runs can be
     sorted together.
  3. to_mermaid / to_d2 / to_g6 — each format consumes the same
     event list. Pin the shape of the output (participants present,
     no naked colons in mermaid, sequence_diagram block in d2,
     nodes+edges in g6).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tracing.server.diagram import (
    Event, resolve_runs, events_from_runs,
    to_mermaid, to_d2, to_g6, build_diagram,
)


# ── Fixture: tiny DB with two runs (agent + linked judge) ────────────────


@pytest.fixture
def db_two_runs(tmp_path):
    """Build a fresh traces.db with one agent run + one judge run
    linked to it via linked_run_id. The agent run has a single
    paired step (one diff_list_files tool call + its result) so the
    event extractor has something to chew on. Returns
    (db_path, agent_run_id, judge_run_id)."""
    db_path = str(tmp_path / "traces.db")
    from orchestra.trace_db import TraceDBWriter

    # Agent run.
    w_a = TraceDBWriter(db_path=db_path, run_id="agent00000001", kind="agent")
    w_a.on_event("agent_started", agent_id="ag1", agent_name="reviewer")
    w_a.on_event(
        "agent_llm_request", agent_id="ag1", agent_name="reviewer", step=0,
        messages=[{"role": "user", "content": "do it"}],
        llm_params={"model": "m", "temperature": 0},
    )
    w_a.on_event(
        "agent_llm_response", agent_id="ag1", agent_name="reviewer", step=0,
        tool_calls=[{"name": "diff_list_files", "arguments": "{}"}],
        content=None,
        usage={"prompt_tokens": 100, "completion_tokens": 20,
               "cached_tokens": 0, "paid": 100},
    )
    # Step 1 — request carries the tool_result message from step 0.
    w_a.on_event(
        "agent_llm_request", agent_id="ag1", agent_name="reviewer", step=1,
        messages=[
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"name": "diff_list_files", "args": "{}"}]},
            {"role": "tool", "content": "M  src/foo.java  (40L · +3/-1 · 1.0kB)"},
        ],
        llm_params={"model": "m", "temperature": 0},
    )
    w_a.on_event(
        "agent_done", agent_id="ag1", agent_name="reviewer", step=1,
        output=[{"file": "src/foo.java", "line": 1,
                 "severity": "MAJOR", "title": "x"}],
    )
    w_a.finish_run(model="m", status="completed")
    w_a.close()

    # Judge run linked to the agent.
    w_j = TraceDBWriter(db_path=db_path, run_id="judge00000001", kind="judge")
    w_j.on_event("agent_started", agent_id="ju1", agent_name="judge")
    w_j.on_event(
        "agent_done", agent_id="ju1", agent_name="judge",
        output={"verdict": "pass", "overall_score": 0.95},
    )
    w_j.finish_run(model="m", status="completed")
    w_j.close()

    # Wire the linked_run_id (and agent_name on the run row) — the
    # writer doesn't have a public method for it, the production
    # path stamps it via the qa_tasks worker.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE runs SET linked_run_id = ?, agent_name = ? WHERE id = ?",
        ("agent00000001", "judge", "judge00000001"),
    )
    conn.execute(
        "UPDATE runs SET agent_name = ? WHERE id = ?",
        ("reviewer", "agent00000001"),
    )
    conn.commit()
    conn.close()

    return db_path, "agent00000001", "judge00000001"


# ── resolve_runs ─────────────────────────────────────────────────────────


class TestResolveRuns:
    def test_session_includes_linked_judge(self, db_two_runs):
        db_path, agent_id, judge_id = db_two_runs
        ids = resolve_runs(f"session://{agent_id}", db_path)
        assert set(ids) == {agent_id, judge_id}, \
            "session scope must include the linked judge run too"

    def test_session_from_judge_side_includes_agent(self, db_two_runs):
        """Reverse direction: starting from the judge id, the resolver
        should also pull in the agent the judge points at."""
        db_path, agent_id, judge_id = db_two_runs
        ids = resolve_runs(f"session://{judge_id}", db_path)
        assert set(ids) == {agent_id, judge_id}

    def test_scenario_run_includes_both(self, db_two_runs):
        """scenario_run_id is the agent's id; the judge points at it
        via linked_run_id. Both must come back."""
        db_path, agent_id, judge_id = db_two_runs
        ids = resolve_runs(f"scenario_run://{agent_id}", db_path)
        assert set(ids) == {agent_id, judge_id}

    def test_unknown_id_returns_empty(self, db_two_runs):
        db_path, _, _ = db_two_runs
        assert resolve_runs("session://does-not-exist", db_path) == []

    def test_unsupported_scheme_raises(self, db_two_runs):
        db_path, _, _ = db_two_runs
        with pytest.raises(ValueError):
            resolve_runs("garbage://x", db_path)

    def test_malformed_uri_raises(self, db_two_runs):
        db_path, _, _ = db_two_runs
        with pytest.raises(ValueError):
            resolve_runs("not_a_uri_at_all", db_path)


# ── events_from_runs ──────────────────────────────────────────────────────


class TestEvents:
    def test_emits_canonical_kinds(self, db_two_runs):
        db_path, agent_id, judge_id = db_two_runs
        events = events_from_runs([agent_id, judge_id], db_path)
        kinds = {e.kind for e in events}
        # tool_call, tool_result emitted from the agent's one paired step;
        # judge_verdict emitted from the judge run.
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        assert "judge_verdict" in kinds

    def test_all_timestamps_tz_aware(self, db_two_runs):
        """Mixing tz-naive and tz-aware datetimes is a TypeError when
        sorting — pin that every event carries tzinfo."""
        db_path, agent_id, judge_id = db_two_runs
        events = events_from_runs([agent_id, judge_id], db_path)
        for e in events:
            assert e.ts.tzinfo is not None, f"naive ts on {e!r}"

    def test_sorted_by_ts(self, db_two_runs):
        db_path, agent_id, judge_id = db_two_runs
        events = events_from_runs([agent_id, judge_id], db_path)
        for a, b in zip(events, events[1:]):
            assert a.ts <= b.ts, f"events not sorted: {a.ts} > {b.ts}"

    def test_actor_uri_shape(self, db_two_runs):
        """Agent actors are `agent:<run_id>:<agent_id>`. Systems are
        `system:<name>`. Both shapes survive _safe_id round-trip
        without colliding."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs([agent_id], db_path)
        for e in events:
            assert e.actor.startswith(("agent:", "system:")), e.actor
            if e.target:
                assert e.target.startswith(("agent:", "system:")), e.target


# ── Renderers ─────────────────────────────────────────────────────────────


class TestParallelCollapse:
    """Parallel tool_calls within ONE LLM step that target the same
    tool name collapse to a single Event with `count`. Spawns and
    reflects never collapse."""

    @pytest.fixture
    def db_parallel(self, tmp_path):
        """One reviewer run, step 0 fires 4 parallel diff_read_file
        calls + 2 diff_outline calls + 1 reflect."""
        db_path = str(tmp_path / "traces.db")
        from orchestra.trace_db import TraceDBWriter
        w = TraceDBWriter(db_path=db_path, run_id="par00000001", kind="agent")
        w.on_event("agent_started", agent_id="ag", agent_name="reviewer")
        # step 0 — parallel calls
        w.on_event(
            "agent_llm_request", agent_id="ag", agent_name="reviewer", step=0,
            messages=[{"role": "user", "content": "go"}],
        )
        w.on_event(
            "agent_llm_response", agent_id="ag", agent_name="reviewer", step=0,
            tool_calls=[
                {"name": "diff_read_file", "arguments": '{"path":"a"}'},
                {"name": "diff_read_file", "arguments": '{"path":"b"}'},
                {"name": "diff_read_file", "arguments": '{"path":"c"}'},
                {"name": "diff_read_file", "arguments": '{"path":"d"}'},
                {"name": "diff_outline",   "arguments": '{"path":"e"}'},
                {"name": "diff_outline",   "arguments": '{"path":"f"}'},
                {"name": "reflect",        "arguments": "{}"},
            ],
        )
        # step 1 — feeds back 6 tool results for the 6 diff_* calls
        # (reflect is agent-self, no result message).
        w.on_event(
            "agent_llm_request", agent_id="ag", agent_name="reviewer", step=1,
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "tool_calls": [{"name": "diff_read_file"}] * 4
                                                    + [{"name": "diff_outline"}] * 2
                                                    + [{"name": "reflect"}]},
                {"role": "tool", "content": "a result"},
                {"role": "tool", "content": "b result"},
                {"role": "tool", "content": "c result"},
                {"role": "tool", "content": "d result"},
                {"role": "tool", "content": "e outline"},
                {"role": "tool", "content": "f outline"},
            ],
        )
        w.on_event(
            "agent_done", agent_id="ag", agent_name="reviewer", step=1,
            output=[],
        )
        w.finish_run(model="m", status="completed")
        w.close()
        return db_path, "par00000001"

    def test_parallel_calls_collapse_with_count(self, db_parallel):
        db_path, rid = db_parallel
        events = events_from_runs([rid], db_path)
        calls = [e for e in events if e.kind == "tool_call"]
        names = [e.label for e in calls]
        # diff_read_file ×4 + diff_outline ×2 = 2 arrows, not 6.
        assert len(calls) == 2, f"expected 2 collapsed call events, got {names}"
        labels = sorted(names)
        assert "diff_outline ×2" in labels[0] or "diff_read_file ×4" in labels[0]
        assert any("×4" in l for l in labels), labels
        assert any("×2" in l for l in labels), labels

    def test_collapsed_count_field(self, db_parallel):
        db_path, rid = db_parallel
        events = events_from_runs([rid], db_path)
        calls = [e for e in events if e.kind == "tool_call"]
        counts = sorted(e.count for e in calls)
        assert counts == [2, 4]

    def test_results_aggregate_in_one_arrow(self, db_parallel):
        """4 diff_read_file + 2 diff_outline = 6 results in step 1's
        tool messages. The collapsed view emits ONE result arrow per
        call group, not six."""
        db_path, rid = db_parallel
        events = events_from_runs([rid], db_path)
        results = [e for e in events if e.kind == "tool_result"]
        assert len(results) == 2, f"expected 2 collapsed result events, got {[r.label for r in results]}"
        labels = [r.label for r in results]
        assert any("4 results" in l for l in labels), labels
        assert any("2 results" in l for l in labels), labels

    def test_reflect_stays_individual(self, db_parallel):
        """reflect / done are agent-self events — they don't fold
        into the diff/bitbucket bucket and never get collapsed even
        though their name is repeatable."""
        db_path, rid = db_parallel
        events = events_from_runs([rid], db_path)
        agent_text = [e for e in events if e.kind == "agent_text"]
        assert len(agent_text) == 1
        assert agent_text[0].label == "reflect"
        assert agent_text[0].count == 1

    def test_payload_keeps_individual_args(self, db_parallel):
        """The collapsed Event's payload preserves each underlying
        argument, so click-through still surfaces the per-call detail
        (even though the diagram label only shows the count)."""
        db_path, rid = db_parallel
        events = events_from_runs([rid], db_path)
        for e in events:
            if e.kind == "tool_call" and "×4" in e.label:
                args = e.payload.get("arguments")
                assert isinstance(args, list)
                assert len(args) == 4
                # Each preserved as raw JSON string.
                assert all('"path"' in a for a in args)
                break
        else:
            raise AssertionError("no ×4 collapse event found")


class TestMermaid:
    def test_starts_with_sequence_diagram(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        assert out.startswith("sequenceDiagram"), out[:80]
        assert "autonumber" in out

    def test_participants_present(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        # Agent + judge + at least one system participant declared.
        assert "as reviewer" in out
        assert "as judge" in out
        assert "as diff" in out  # diff_list_files was the only tool call

    def test_no_naked_colons_in_message_labels(self, db_two_runs):
        """Mermaid parser dies on stray `:` inside arrow labels. We
        replace it with a middle-dot. Pin: no `:` after `: `
        (which is what arrows look like) other than the participant
        separator."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        for line in out.splitlines():
            if "->>" in line or "-->>" in line:
                # After the first `: ` (label start), no further `:`
                # allowed.
                idx = line.find(": ")
                if idx >= 0:
                    label = line[idx + 2:]
                    assert ":" not in label, \
                        f"naked colon in mermaid label: {line!r}"

    def test_empty_scope_gives_placeholder(self, db_two_runs):
        db_path, _, _ = db_two_runs
        out = to_mermaid([], db_path)
        assert out.startswith("sequenceDiagram"), out


class TestD2:
    def test_emits_sequence_diagram_block(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_d2(events, db_path)
        assert "shape: sequence_diagram" in out
        assert "seq: {" in out
        assert out.rstrip().endswith("}")

    def test_labels_are_quoted(self, db_two_runs):
        """D2 needs quoted labels when the value contains special
        chars (`:`, `'`, etc.). We `json.dumps` every label — pin
        that every message line ends with a balanced `"…"`."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_d2(events, db_path)
        for line in out.splitlines():
            if "->" in line and ":" in line.split("->", 1)[1]:
                # Label after the last `: ` should start with `"` and
                # end before `}` with `"`.
                stripped = line.rstrip()
                # tolerate trailing `}` on the closing line itself
                if stripped == "}":
                    continue
                # find the label segment after `: `
                idx = stripped.rfind(": ")
                if idx >= 0:
                    label = stripped[idx + 2:]
                    assert label.startswith('"') and label.endswith('"'), \
                        f"unquoted d2 label: {line!r}"


class TestG6:
    def test_returns_nodes_and_edges(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_g6(events, db_path)
        assert isinstance(out, dict)
        assert "nodes" in out and "edges" in out
        assert isinstance(out["nodes"], list)
        assert isinstance(out["edges"], list)

    def test_edge_weights_aggregate_requests_and_responses(self, db_two_runs):
        """tool_call (agent → system) and tool_result (system → agent)
        should fold into ONE edge in each direction. We made
        `tool_result` flip the edge direction so it merges with the
        outgoing edge — pin that the agent → system edge counts both
        the call and the response."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_g6(events, db_path)
        # Find the agent → diff system edge.
        agent_to_diff = [
            e for e in out["edges"]
            if e["source"].startswith("agent_") and e["target"].endswith("_diff")
        ]
        assert agent_to_diff, f"no agent→diff edge in {out['edges']}"
        # Call + result = at least 2 events folded into one edge.
        assert agent_to_diff[0]["weight"] >= 2

    def test_node_kinds_classified(self, db_two_runs):
        """Each node carries a `kind` ∈ {agent, system, human} so the
        UI can colour lanes consistently across formats."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_g6(events, db_path)
        kinds = {n["kind"] for n in out["nodes"]}
        assert "agent" in kinds
        assert "system" in kinds


# ── Top-level build_diagram (endpoint shape) ─────────────────────────────


class TestBuildDiagram:
    def test_mermaid_returns_text(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        mime, body = build_diagram(
            f"session://{agent_id}", "mermaid", db_path,
        )
        assert mime.startswith("text/plain")
        assert isinstance(body, str) and body.startswith("sequenceDiagram")

    def test_d2_returns_text(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        mime, body = build_diagram(
            f"session://{agent_id}", "d2", db_path,
        )
        assert mime.startswith("text/plain")
        assert isinstance(body, str)

    def test_g6_returns_dict(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        mime, body = build_diagram(
            f"session://{agent_id}", "g6", db_path,
        )
        assert mime == "application/json"
        assert isinstance(body, dict)
        assert "nodes" in body and "edges" in body

    def test_unsupported_format_raises(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        with pytest.raises(ValueError):
            build_diagram(f"session://{agent_id}", "ascii_art", db_path)
