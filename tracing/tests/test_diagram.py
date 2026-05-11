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


class TestTimestampOrdering:
    """Sibling spawn-done arrows + child events emitted via DFS must
    end up monotonically ordered after the collector assigns ts.
    Previously the parent's `step_offset_ms` was local: a spawn at
    parent ts=250 walked the child whose events ran to ts=1000+,
    then the parent's done was written at ts≈251 and its NEXT
    step at ts=300 — both BEFORE the child's last event. After
    sorting that scrambled the autonumber on Mermaid."""

    @pytest.fixture
    def db_with_spawn(self, tmp_path):
        db_path = str(tmp_path / "traces.db")
        from orchestra.trace_db import TraceDBWriter
        # Parent reviewer: 3 steps, in step 1 it spawns an
        # investigator that itself runs 4 steps before done'ing.
        w = TraceDBWriter(db_path=db_path, run_id="parent0001", kind="agent")
        w.on_event("agent_started", agent_id="P", agent_name="reviewer")
        # step 0
        w.on_event("agent_llm_request", agent_id="P", agent_name="reviewer", step=0, messages=[])
        w.on_event("agent_llm_response", agent_id="P", agent_name="reviewer", step=0,
                   tool_calls=[{"name": "diff_list_files", "arguments": "{}"}])
        # step 1 — request carries tool result for step 0, then a spawn_agent
        w.on_event("agent_llm_request", agent_id="P", agent_name="reviewer", step=1,
                   messages=[{"role": "tool", "content": "ok"}])
        w.on_event("agent_spawned", agent_id="P", agent_name="reviewer", step=1,
                   data={"child_id": "C", "parent_id": "P", "agent_name": "investigator"})
        w.on_event("agent_llm_response", agent_id="P", agent_name="reviewer", step=1,
                   tool_calls=[{"name": "spawn_agent",
                                "arguments": '{"agent":"investigator","focus":"x"}'}])
        # CHILD runs steps 0..3 — emitted before parent's step 2
        for s in range(4):
            w.on_event("agent_llm_request", agent_id="C", agent_name="investigator",
                       step=s, messages=[])
            w.on_event("agent_llm_response", agent_id="C", agent_name="investigator",
                       step=s,
                       tool_calls=[{"name": "diff_read_file",
                                    "arguments": f'{{"path":"file{s}"}}'}])
        w.on_event("agent_done", agent_id="C", agent_name="investigator", step=3,
                   output=[{"file": "x", "line": 1,
                             "severity": "MAJOR", "title": "t"}])
        # PARENT step 2 — happens AFTER the child has finished
        w.on_event("agent_llm_request", agent_id="P", agent_name="reviewer", step=2,
                   messages=[])
        w.on_event("agent_llm_response", agent_id="P", agent_name="reviewer", step=2,
                   tool_calls=[{"name": "post_comment",
                                "arguments": '{"text":"done"}'}])
        w.on_event("agent_done", agent_id="P", agent_name="reviewer", step=3,
                   output=[])
        w.finish_run(model="m", status="completed")
        w.close()
        return db_path, "parent0001"

    def test_events_monotonic_ts(self, db_with_spawn):
        """Real timestamps are used; events emitted at the same
        microsecond can have equal `ts`. Sort stability + DFS
        emission order keeps the *visual* order correct without any
        synthetic nudge — we just pin non-decreasing ts here."""
        db_path, rid = db_with_spawn
        events = events_from_runs([rid], db_path)
        for a, b in zip(events, events[1:]):
            assert a.ts <= b.ts, \
                f"events ts not monotonic: {a.label} @ {a.ts} > {b.label} @ {b.ts}"

    def test_child_events_between_spawn_and_done(self, db_with_spawn):
        """The investigator's events MUST appear between the
        reviewer's `agent_spawn` arrow and the corresponding
        `agent_done` return. This pins the DFS ordering."""
        db_path, rid = db_with_spawn
        events = events_from_runs([rid], db_path)
        spawn_idx = next(i for i, e in enumerate(events)
                         if e.kind == "agent_spawn")
        done_idx = next(i for i, e in enumerate(events)
                        if e.kind == "agent_done"
                        and e.target and "P" in e.target)
        child_tool_calls = [i for i, e in enumerate(events)
                            if e.kind == "tool_call"
                            and "C" in e.actor]
        assert child_tool_calls, "child made no tool calls?"
        for i in child_tool_calls:
            assert spawn_idx < i < done_idx, \
                f"child event at idx {i} not bracketed by spawn={spawn_idx} done={done_idx}"

    def test_parent_step_after_done_comes_after_child(self, db_with_spawn):
        """The reviewer's `post_comment` (step 2, after spawn) MUST
        come after the investigator's done — the bug we're fixing
        was that it sorted ahead by milliseconds."""
        db_path, rid = db_with_spawn
        events = events_from_runs([rid], db_path)
        # Find post_comment by parent
        post_idx = next(i for i, e in enumerate(events)
                        if "post_comment" in e.label)
        done_idx = next(i for i, e in enumerate(events)
                        if e.kind == "agent_done" and "C" in e.actor)
        assert post_idx > done_idx, \
            f"parent's post_comment at {post_idx} not after child's done at {done_idx}"


class TestStepLabels:
    def test_step_prefix_in_mermaid_label(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"session://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        # Every tool_call / tool_result arrow carries a [s<N>] prefix.
        arrow_lines = [l for l in out.splitlines()
                       if ("->>" in l or "-->>" in l) and l.split(": ", 1)[-1].strip()]
        assert arrow_lines, "no arrows in mermaid output"
        with_step = [l for l in arrow_lines if "[s" in l]
        assert with_step, f"no [sN] prefix in any arrow: {arrow_lines[:3]}"

    def test_no_global_autonumber(self, db_two_runs):
        """We dropped Mermaid's `autonumber` so per-agent step
        numbers (in labels) are the only counter visible to the
        reader — preventing the misleading 'event #18 is after
        event #16' confusion when sequences interleave."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"session://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        assert "autonumber" not in out, \
            "Mermaid autonumber re-enabled — would conflict with per-agent step labels"


class TestFairCollapse:
    @pytest.fixture
    def db_chatty(self, tmp_path):
        """One run with 20 sequential diff_read_file calls across
        20 steps — naturally produces 40 events (20 calls + 20
        results), well above any reasonable max_events default."""
        db_path = str(tmp_path / "traces.db")
        from orchestra.trace_db import TraceDBWriter
        w = TraceDBWriter(db_path=db_path, run_id="chatty0001", kind="agent")
        w.on_event("agent_started", agent_id="A", agent_name="reviewer")
        for s in range(20):
            w.on_event("agent_llm_request", agent_id="A", agent_name="reviewer",
                       step=s, messages=[])
            w.on_event("agent_llm_response", agent_id="A", agent_name="reviewer",
                       step=s,
                       tool_calls=[{"name": "diff_read_file",
                                    "arguments": f'{{"path":"f{s}"}}'}])
        # Final request carries last tool result so step 19's call gets paired.
        w.on_event("agent_llm_request", agent_id="A", agent_name="reviewer",
                   step=20,
                   messages=[{"role": "tool", "content": "result_19"}])
        w.on_event("agent_done", agent_id="A", agent_name="reviewer", step=20,
                   output=[])
        w.finish_run(model="m", status="completed")
        w.close()
        return db_path, "chatty0001"

    def test_collapse_to_budget(self, db_chatty):
        db_path, rid = db_chatty
        full = events_from_runs([rid], db_path)
        assert len(full) >= 20, "expected many events without collapse"
        collapsed = events_from_runs([rid], db_path, max_events=10)
        assert len(collapsed) <= len(full)
        # Should fold the 20 adjacent diff_read_file calls into one
        # high-count event.
        calls = [e for e in collapsed if e.kind == "tool_call"]
        assert any(e.count >= 5 for e in calls), \
            f"no ×N collapse fired: {[e.label for e in calls]}"

    def test_no_collapse_under_budget(self, db_chatty):
        """A small budget triggers folding; a generous one leaves
        events alone."""
        db_path, rid = db_chatty
        evs_loose = events_from_runs([rid], db_path, max_events=10**6)
        evs_default = events_from_runs([rid], db_path)
        assert len(evs_loose) == len(evs_default)

    def test_spawn_done_never_fold(self, db_chatty):
        """Folding budget aggression must NOT erase agent_spawn /
        agent_done / agent_text events. Pin with a very tight
        budget."""
        db_path, _ = db_chatty
        # Use the spawn fixture instead for this — but reuse the
        # mechanism. We're testing _fold_one_round's blacklist.
        e_spawn = Event(ts=datetime.now(timezone.utc), kind="agent_spawn",
                        actor="agent:r:p", target="agent:r:c",
                        label="spawn(x)", count=1)
        e_spawn2 = Event(ts=datetime.now(timezone.utc), kind="agent_spawn",
                         actor="agent:r:p", target="agent:r:c",
                         label="spawn(y)", count=1)
        from tracing.server.diagram import _foldable
        assert not _foldable(e_spawn, e_spawn2), \
            "spawns folded — would lose distinct child focus arguments"


class TestMermaid:
    def test_starts_with_sequence_diagram(self, db_two_runs):
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        assert out.startswith("sequenceDiagram"), out[:80]
        # NOTE: no `autonumber` directive — per-agent step numbers
        # are surfaced as `[sN]` prefixes in each event label
        # instead (see TestStepLabels). Mermaid's global counter
        # was misleading when sequences from multiple agents
        # interleave on the timeline.

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

    def test_balanced_activation(self, db_two_runs):
        """Mermaid trips on `Trying to inactivate an inactive
        participant` when `-->>-` fires without a matching `->>+`.
        Pin: every `-->>-` must be preceded by a `->>+` on the
        same id earlier in the output."""
        db_path, _, _ = db_two_runs
        # Use a synthetic stream that has an agent_done with no
        # matching agent_spawn — the renderer must downgrade it.
        events = [
            Event(ts=datetime.now(timezone.utc), kind="agent_done",
                  actor="agent:r:orphan", target="agent:r:p",
                  label="done", session_id="r"),
        ]
        out = to_mermaid(events, db_path)
        # No `-->>-` should appear — unbalanced events get the
        # plain dashed form instead.
        assert "-->>-" not in out, \
            f"unbalanced agent_done rendered as `-->>-`: {out}"

    def test_role_boxes_in_mermaid(self, db_two_runs):
        """Participants must be wrapped in `box rgb(...)` groups,
        one per role (Reviewer / Investigators / Tools / Judge).
        Box colouring is mermaid's only first-class per-actor
        styling — without it the diagram would render with all
        lanes the same blue."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_mermaid(events, db_path)
        assert "  box rgb(" in out, \
            f"no role box wrapping in mermaid output:\n{out[:400]}"
        # `end` closes each box; count should match boxes.
        boxes = sum(1 for l in out.splitlines() if l.strip().startswith("box "))
        ends  = sum(1 for l in out.splitlines() if l.strip() == "end")
        assert boxes == ends, f"unbalanced box/end: {boxes} boxes vs {ends} ends"

    def test_d2_uses_classes_for_palette(self, db_two_runs):
        """D2 declares role palette as `classes:` and each actor
        carries `class: <role>` — references not inlined per actor.
        Critical for compactness: inline style on every participant
        blew the play.d2lang.com URL past CloudFront's 8KB cap.
        Pin: `classes:` block present, each actor line has `class:`,
        no inlined `style.fill` on actors."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_d2(events, db_path)
        assert "classes: {" in out
        assert "fill:" in out  # inside the classes block
        assert "stroke:" in out
        # Actor lines (inside seq: {…}) reference the class, NOT inline
        # style. A single actor line shape: `<alias>: {label: "…"; class: <role>}`.
        actor_lines = [l for l in out.splitlines()
                       if "{label:" in l and "class:" in l]
        assert actor_lines, "no actor lines with class:"
        for l in actor_lines:
            assert "style.fill" not in l, \
                f"actor line still has inline style: {l}"

    def test_g6_nodes_carry_role_colors(self, db_two_runs):
        """G6 nodes get `fill` and `stroke` from the shared palette
        so the frontend doesn't need to know about roles to render
        consistently with mermaid/d2."""
        db_path, agent_id, _ = db_two_runs
        events = events_from_runs(
            resolve_runs(f"scenario_run://{agent_id}", db_path), db_path,
        )
        out = to_g6(events, db_path)
        for n in out["nodes"]:
            assert "fill" in n, f"missing fill on g6 node: {n}"
            assert "stroke" in n, f"missing stroke on g6 node: {n}"
            assert "role" in n, f"missing role on g6 node: {n}"
            assert n["fill"].startswith("#"), n
            assert n["stroke"].startswith("#"), n

    def test_open_activation_gets_closed(self, db_two_runs):
        """If the run is mid-flight (spawn arrived but no done yet),
        any still-activated lanes get a synthetic `deactivate` at
        the end so Mermaid doesn't warn about open activations."""
        db_path, _, _ = db_two_runs
        ts = datetime.now(timezone.utc)
        events = [
            Event(ts=ts, kind="agent_spawn",
                  actor="agent:r:p", target="agent:r:c",
                  label="spawn(x)", session_id="r"),
        ]
        out = to_mermaid(events, db_path)
        # Spawn produces `->>+` — the child lane is left open.
        # The renderer should append a `deactivate <child>` at end.
        assert "->>+" in out
        # The synthetic close-out for the still-active child.
        assert "deactivate " in out


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
