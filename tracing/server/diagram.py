"""
Scope-agnostic interaction-diagram builder.

Single contract: caller passes a resource URI, builder resolves it
transitively to a set of run_ids, extracts a canonical event stream,
and hands it to one of three renderers.

  GET /api/diagram?scope=<uri>&format=mermaid|d2|g6

  resolve_runs(scope_uri) → list[run_id]
       │
       ▼
  events_from_runs(run_ids) → list[Event]   # canonical, sorted by ts
       │
       ├─→ to_mermaid(events)  →  sequenceDiagram source (inline render)
       ├─→ to_d2(events)       →  D2 source (savable/editable text)
       └─→ to_g6(events)       →  node-edge JSON (force-directed graph)

URI schemes supported NOW:
  session://<run_id>        — one cli.py invocation
  scenario_run://<id>       — agent + linked judge for one attempt

URI schemes the resolver is ready for (TODO data sources):
  plan://<plan_id>          — all runs in a plan (queue side)
  pr://<pr_url>             — all runs for a PR over its lifetime
                              (needs human comments + push events
                               from bitbucket + git activity APIs)
  mutation://<sha>          — all runs against this commit

Renderer notes:
  - Mermaid: inline-rendered sequenceDiagram with `click NodeId callback`
    so tool calls / spawns / dones open the payload tab on right panel.
  - D2: source-only. Inline render needs d2-js (~1.5MB); we ship the
    text + a `play.d2lang.com` link so it's `save → reopen → edit`,
    which was the main reason to bring D2 in.
  - AntV G6: node-edge view, not sequence — loses the time axis but
    is compact for sessions with many tool calls (one node per
    agent / system, edge weight = call count). Same JSON works for
    plan / PR scopes when they land.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ── Canonical event ───────────────────────────────────────────────────────


@dataclass
class Event:
    """One thing that happens on the timeline. Scope-agnostic: the
    same shape comes from session, scenario_run, plan, pr scopes.

    `count > 1` means this event represents N collapsed sibling
    calls (parallel tool_calls within one LLM step that resolved to
    the same tool name). Renderers SHOULD use `count` to annotate
    the label (`diff_read_file × 3` instead of three identical
    arrows). Spawns and done's never collapse — each is a distinct
    control-flow handoff.
    """
    ts: Optional[datetime]  # assigned monotonically by the collector
    kind: str               # tool_call | tool_result | agent_spawn |
                            # agent_done   (a text-only LLM step
                            # emits as tool_call with target=system:
                            # human, no paired result — same shape
                            # as any other tool call, no kind-special
                            # case)
    actor: str              # "agent:<run_id>:<agent_id>" |
                            # "system:<name>" (e.g. system:human,
                            # system:diff, system:bitbucket,
                            # system:trunc)
    target: Optional[str]   # same alphabet as actor
    label: str              # short display string
    payload: dict = field(default_factory=dict)   # full data for click → side panel
    session_id: Optional[str] = None              # which run this belongs to
    count: int = 1          # > 1 ⇒ collapsed parallel/adjacent siblings
    step: Optional[int] = None  # LLM step within the *agent* that owns
                                # this event. Per-agent counter, NOT
                                # the global autonumber Mermaid would
                                # produce. Renderers surface as
                                # `[s<step>]` prefix so the reader can
                                # cross-reference with the agent tree.


# ── Tool → system bucket ──────────────────────────────────────────────────

_SYSTEM_FOR: dict[str, str] = {
    "diff_list_files":  "diff",
    "diff_read_file":   "diff",
    "diff_outline":     "diff",
    "diff_search":      "diff",
    "list_threads":      "bitbucket",
    "read_thread":       "bitbucket",
    "read_comment":      "bitbucket",
    "post_comment":      "bitbucket",
    "set_review_status": "bitbucket",
    # Control-flow primitives don't route through a system lane:
    #   spawn_agent       → agent_spawn event (parent → child)
    #   done (child)      → agent_done event (child → parent),
    #                       the same event as the spawn's return
    #   done (root) / reflect → self-loop tool_call (actor == target)
}


def _system_for(tool_name: str) -> str:
    return _SYSTEM_FOR.get(tool_name, "other")


# ── Scope resolver ────────────────────────────────────────────────────────


def resolve_runs(scope_uri: str, db_path: str) -> list[str]:
    """Resource URI → ordered list of run_ids in its transitive closure."""
    scheme, _, rest = scope_uri.partition("://")
    if not scheme or not rest:
        raise ValueError(f"malformed scope URI: {scope_uri}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if scheme == "session":
            return _closure_for_session(conn, rest)
        if scheme == "scenario_run":
            return _closure_for_scenario_run(conn, rest)
        if scheme == "plan":
            return _runs_for_plan(conn, int(rest))
        # Future URI schemes plug in here. The renderer & UI don't
        # need to change — just add a resolver branch.
        #   pr://<url>      → SELECT id FROM runs WHERE pr_url=? plus
        #                      human comments + push events from
        #                      bitbucket / git activity (separate
        #                      tables once we capture them).
        #   mutation://<sha> → SELECT id FROM runs WHERE mutation=?
        raise ValueError(f"unsupported scope URI scheme: {scheme}")
    finally:
        conn.close()


def _closure_for_session(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """One session + its linked partner (agent ↔ judge). The judge is a
    separate cli.py run that scores the agent's output; it shares
    scenario_run_id via linked_run_id."""
    seed = conn.execute(
        "SELECT id, linked_run_id FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if not seed:
        return []
    ids = {seed["id"]}
    if seed["linked_run_id"]:
        ids.add(seed["linked_run_id"])
    # Also: runs that point at THIS run via linked_run_id.
    for r in conn.execute(
        "SELECT id FROM runs WHERE linked_run_id = ?", (run_id,)
    ):
        ids.add(r["id"])
    return _ordered(conn, ids)


def _closure_for_scenario_run(conn: sqlite3.Connection, sr_id: str) -> list[str]:
    """scenario_run_id is the agent's own id; the judge points at it
    via linked_run_id. Both runs together = one attempt at the
    scenario."""
    ids = set()
    for r in conn.execute(
        "SELECT id FROM runs WHERE id = ? OR linked_run_id = ?",
        (sr_id, sr_id),
    ):
        ids.add(r["id"])
    return _ordered(conn, ids)


def _runs_for_plan(conn: sqlite3.Connection, plan_id: int) -> list[str]:
    """All runs whose qa_tasks row carries this plan_id. Includes
    agent and judge sessions across every scenario × provider × repeat."""
    ids = set()
    for r in conn.execute(
        "SELECT DISTINCT trace_run_id FROM qa_tasks "
        "WHERE plan_id = ? AND trace_run_id IS NOT NULL",
        (plan_id,),
    ):
        ids.add(r[0])
    return _ordered(conn, ids)


def _ordered(conn: sqlite3.Connection, ids: set[str]) -> list[str]:
    """Sort run_ids by started_at — gives the timeline a stable order
    when multiple runs share a scope."""
    if not ids:
        return []
    qmarks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM runs WHERE id IN ({qmarks}) ORDER BY started_at",
        list(ids),
    ).fetchall()
    return [r[0] for r in rows]


# ── Event extraction from one run ─────────────────────────────────────────


def events_from_runs(run_ids: list[str], db_path: str,
                     *, max_events: Optional[int] = None,
                     actor_filter: Optional[list[str]] = None,
                     edge_filter: Optional[list[tuple[str, str]]] = None,
                     ) -> list[Event]:
    """Build the canonical event stream for every run in scope, sort
    by timestamp. Filters apply BEFORE fair-collapse so the budget
    accounts for the filtered subset, not the pre-filtered total.

    `actor_filter` — URIs of actors of interest. An event passes if
    EITHER its `actor` OR its `target` is in the list (union of
    "everything touching X" for each X). Use the safe_id-decoded
    canonical URIs (e.g. `agent:<run>:<aid>`, `system:diff`).

    `edge_filter` — list of (source_uri, target_uri) tuples. An event
    passes if its (actor, target) matches one of the pairs in either
    direction (since e.g. tool_result reverses the arrow vs the
    original tool_call).

    If BOTH filters are supplied: the event must satisfy both
    (intersection — "interactions between A and B that also touch
    actor X"). That's the natural UX when a user clicks an edge AND
    a node in G6.
    """
    out: list[Event] = []
    for rid in run_ids:
        out.extend(_events_from_run(rid, db_path))
    out.sort(key=lambda e: e.ts)

    if actor_filter:
        af = set(actor_filter)
        out = [e for e in out if e.actor in af or (e.target and e.target in af)]
    if edge_filter:
        ef = set()
        for src, tgt in edge_filter:
            ef.add((src, tgt)); ef.add((tgt, src))
        out = [e for e in out
               if (e.actor, e.target) in ef]

    if max_events and len(out) > max_events:
        out = _fair_collapse(out, max_events)
    return out


def _name_stem(label: str) -> str:
    """Strip args, ×N suffix, and [s<N>] prefix so we can compare
    tool-name identity across events: `[s3] diff_read_file ×4` →
    `diff_read_file`."""
    s = label
    if s.startswith("[s") and "] " in s:
        s = s.split("] ", 1)[1]
    if " ×" in s:
        s = s.split(" ×", 1)[0]
    if "(" in s:
        s = s.split("(", 1)[0]
    return s.strip()


def _foldable(a: Event, b: Event) -> bool:
    """Adjacent events fold together iff same direction, same actors,
    same kind, same tool stem. Spawn/done never fold — collapsing
    those erases control-flow milestones."""
    if a.kind != b.kind:
        return False
    if a.kind in ("agent_spawn", "agent_done"):
        return False
    if a.actor != b.actor or a.target != b.target:
        return False
    return _name_stem(a.label) == _name_stem(b.label)


def _foldable_pair(a: Event, b: Event) -> bool:
    """Loose-fold: same (actor, target, kind) — collapses bundles
    of different tool names that all go to the same system.
    `agent → diff` carries `diff_read_file ×4` + `diff_search ×3` +
    `diff_outline ×2` in successive emit slots; this predicate
    lets them merge into one bundle event with a stacked label."""
    if a.kind != b.kind:
        return False
    if a.kind in ("agent_spawn", "agent_done"):
        return False
    return a.actor == b.actor and a.target == b.target


def _fold_one_round(events: list[Event]) -> list[Event]:
    """One pass: merge each adjacent foldable pair. Mutates fold targets
    in place — count summed, label rewritten with ×N suffix."""
    out: list[Event] = []
    for e in events:
        if out and _foldable(out[-1], e):
            prev = out[-1]
            prev.count += e.count
            stem = _name_stem(prev.label)
            prev.label = f"{stem} ×{prev.count}"
            # Preserve all underlying payloads so click-through still
            # surfaces the per-call detail.
            prev_payload = prev.payload or {}
            merged = list(prev_payload.get("merged", [prev_payload]))
            merged.append(e.payload or {})
            prev.payload = {**prev_payload, "merged": merged}
            continue
        out.append(e)
    return out


def _fold_pair_bundles(events: list[Event]) -> list[Event]:
    """One pass: merge each adjacent same-(actor,target,kind) pair
    regardless of tool name. Sub-labels accumulate in
    `payload.bundle` so click-through and multi-line rendering
    preserve detail; compact `e.label` shows `first + N more`.

    Flattens nested bundles — if `e` itself is already a bundle,
    its sub-labels are spliced in (otherwise `e.label` would be
    `"X + 2 more"` and that compact string would appear as ONE
    line in the parent bundle, hiding the underlying ops)."""
    out: list[Event] = []
    for e in events:
        if out and _foldable_pair(out[-1], e):
            prev = out[-1]
            prev_payload = prev.payload or {}
            bundle = list(prev_payload.get("bundle") or [prev.label])
            e_payload = e.payload or {}
            e_bundle = e_payload.get("bundle")
            if e_bundle:
                bundle.extend(e_bundle)
            else:
                bundle.append(e.label)
            prev.count += e.count
            first = _name_stem(bundle[0])
            n_more = len(bundle) - 1
            prev.label = (f"{first} + {n_more} more" if n_more
                          else first)
            prev.payload = {**prev_payload, "bundle": bundle}
            continue
        out.append(e)
    return out


def _fair_collapse(events: list[Event], max_events: int) -> list[Event]:
    """Fold per run, fair-share, progressive — each run gets
    `max_events / N` slots; runs already inside their share aren't
    touched. Three escalating stages until the group fits:

      1. Adjacent same-stem fold (within run, across steps). Folds
         `call(diff_read_file) → call(diff_read_file)` into one ×N.
      2. Drop tool_result events. Real-life chatter alternates
         call/result/call/result/… so adjacent fold can't merge
         calls separated by a result. Once results are gone, stage 1
         runs again and the remaining calls collapse.
      3. If still over, give up gracefully — the renderer's own cap
         truncates the tail with a Note.
    """
    from collections import defaultdict
    groups: dict[str, list[Event]] = defaultdict(list)
    order: list[str] = []
    for e in events:
        sid = e.session_id or ""
        if sid not in groups:
            order.append(sid)
        groups[sid].append(e)
    n_groups = max(1, len(groups))
    target_per_run = max(8, max_events // n_groups)

    def _fold_until_fixed(evs: list[Event],
                          pass_fn=_fold_one_round) -> list[Event]:
        prev_len = len(evs) + 1
        while len(evs) > target_per_run and len(evs) < prev_len:
            prev_len = len(evs)
            evs = pass_fn(evs)
        return evs

    for sid in order:
        # Stage 1: adjacent same-stem fold.
        evs = _fold_until_fixed(groups[sid])
        # Stage 1.5: adjacent same-(actor,target,kind) bundle fold —
        # different tool names that go to the same target lane in a
        # row become one "bundle" event. Big wins when an agent
        # pings a system with diff_read_file, diff_outline, diff_search
        # in successive steps.
        if len(evs) > target_per_run:
            evs = _fold_until_fixed(evs, pass_fn=_fold_pair_bundles)
        # Stage 2: drop results, re-fold (both strict and loose).
        if len(evs) > target_per_run:
            evs = [e for e in evs if e.kind != "tool_result"]
            evs = _fold_until_fixed(evs)
            if len(evs) > target_per_run:
                evs = _fold_until_fixed(evs, pass_fn=_fold_pair_bundles)
        # Stage 3: hard truncate. User asked for ≤ N events — at
        # this point folding has plateaued. Drop the tail and emit
        # a sentinel tool_call to system:trunc so the diagram
        # visibly shows the budget was hit. The sentinel goes into
        # the canonical Event stream; renderers handle it via the
        # generic tool_call path (no kind-special case needed).
        if len(evs) > target_per_run and target_per_run > 0:
            anchor = evs[0].actor if evs else "system:trunc"
            anchor_ts = evs[target_per_run - 1].ts if evs else None
            evs = evs[: target_per_run - 1]
            evs.append(Event(
                ts=anchor_ts, kind="tool_call",
                actor=anchor, target="system:trunc",
                label=f"⋯ truncated at {target_per_run} events ⋯",
                payload={"truncated": True},
                session_id=sid,
            ))
        groups[sid] = evs

    out: list[Event] = []
    for sid in order:
        out.extend(groups[sid])
    out.sort(key=lambda e: e.ts)
    return out


def _events_from_run(run_id: str, db_path: str) -> list[Event]:
    """One run → list of Events. Reuses the prepared tree
    (`orchestra.trace._prepare_agent`) for the agent hierarchy and
    paired_step grouping, but pulls REAL per-event timestamps from
    the `events` table so the diagram shows actual scope-open /
    scope-close moments:

      tool_call(step N)   → ts of agent_llm_response(agent, N),
                            or agent_llm_request when no response
                            preceded (mode:single — text-only step)
      tool_result(step N) → ts of agent_llm_request(agent, N+1)
      agent_spawn         → ts of child's agent_started
      agent_done (return) → ts of child's agent_done event

    Missing timestamps fall back to the previous event's ts +
    1 microsecond so the stream stays monotonic on degenerate data
    (e.g., agent_done without a matching agent_started event).
    """
    from orchestra.trace_db import TraceDBReader
    from orchestra.trace import _prepare_agent

    reader = TraceDBReader(db_path=db_path)
    try:
        raw = reader.get_run_trace(run_id)
        if not raw:
            return []
        prepared = _prepare_agent(raw, depth=0)
        meta = _run_meta(db_path, run_id)
    finally:
        reader.close()
    if not prepared:
        return []

    kind = meta.get("kind") or "agent"
    ts_index = _load_real_ts(run_id, db_path)

    out: list[Event] = []
    _walk_agent(prepared, run_id=run_id, parent_actor=None,
                out=out, run_kind=kind)
    _assign_real_ts(out, ts_index)
    # Drop events whose real timestamp wasn't available — typically
    # `agent_done` arrows for children of a still-running session, or
    # `tool_result` for the last in-flight step. Showing them with a
    # synthesised ts piled them all at run-start, which made the
    # timeline read inside-out.
    return [e for e in out if e.ts is not None]


def _load_real_ts(run_id: str, db_path: str) -> dict:
    """Build (agent_id, step, event_type) → datetime + agent_id →
    (started_at, done_at) bounds from the events table for `run_id`.

    The events table is small per-run (tens to a few hundred rows)
    so a single SELECT + dict build is the cheap path.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT agent_id, step, event_type, timestamp "
            "FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    keyed: dict[tuple, datetime] = {}
    bounds: dict[str, dict] = {}  # agent_id -> {"started", "done"}
    for aid, step, etype, ts in rows:
        if not ts:
            continue
        parsed = _parse_ts(ts)
        keyed[(aid, step, etype)] = parsed
        if etype == "agent_started":
            bounds.setdefault(aid, {})["started"] = parsed
        elif etype == "agent_done":
            bounds.setdefault(aid, {})["done"] = parsed
    return {"keyed": keyed, "bounds": bounds}


def _agent_id_from_actor(actor: str) -> Optional[str]:
    """Extract the agent_id segment from `agent:<run_id>:<agent_id>`."""
    if not actor or not actor.startswith("agent:"):
        return None
    parts = actor.split(":")
    return parts[2] if len(parts) >= 3 else None


def _assign_real_ts(events: list[Event], ts_index: dict) -> None:
    """Walk events in DFS-emission order; resolve each to a real ts
    via `ts_index` based on its kind + actor + step. Events without
    a real ts get `e.ts = None` — the caller filters those out so
    nothing ends up at a synthesised "run start" position (which
    used to bury the real timeline under a wall of placeholder
    events at the top of the diagram)."""
    keyed = ts_index["keyed"]
    bounds = ts_index["bounds"]

    for e in events:
        aid = _agent_id_from_actor(e.actor)
        tgt_aid = _agent_id_from_actor(e.target) if e.target else None
        # tool_result events have actor=system:..., target=agent:... —
        # the agent that received the result is the relevant one for
        # ts lookup (its NEXT step's request carries the result).
        agent_party = aid if aid else tgt_aid
        resolved: Optional[datetime] = None

        if e.kind == "tool_call" and aid and e.step is not None:
            # Tool call ts = when the LLM decided to call (its
            # response). Mode:single agents (judges, lead agents
            # that don't loop) have no preceding response — fall
            # back to the request ts so the call is still placed
            # in the timeline.
            resolved = (keyed.get((aid, e.step, "agent_llm_response"))
                        or keyed.get((aid, e.step, "agent_llm_request")))
        elif e.kind == "tool_result" and agent_party and e.step is not None:
            resolved = (keyed.get((agent_party, e.step + 1,
                                    "agent_llm_request"))
                        or keyed.get((agent_party, e.step,
                                       "agent_llm_response")))
        elif e.kind == "agent_spawn" and tgt_aid:
            resolved = (bounds.get(tgt_aid, {}).get("started")
                        or (keyed.get((aid, e.step, "agent_llm_response"))
                            if aid and e.step is not None else None))
        elif e.kind == "agent_done":
            child_id = _agent_id_from_actor(e.actor)
            if child_id:
                resolved = bounds.get(child_id, {}).get("done")

        e.ts = resolved  # may be None — caller filters those out


def _walk_agent(agent: dict, *, run_id: str,
                parent_actor: Optional[str],
                out: list[Event],
                run_kind: str) -> None:
    """Recurse the prepared tree, appending Events in emission order.
    ts is left None — the caller assigns monotonic timestamps once
    the whole walk is done."""
    aid = agent.get("agent_id") or "?"
    actor_self = f"agent:{run_id}:{aid}"

    children_by_id = {
        (c.get("agent_id") or ""): c for c in (agent.get("children") or [])
    }
    children_in_order = list(agent.get("children") or [])
    spawn_used: set[str] = set()

    for step in (agent.get("paired_steps") or []):
        step_num = step.get("step")
        resp = step.get("resp") or {}
        tool_calls = resp.get("tool_calls") or []
        tool_results = step.get("tool_results") or []

        # Group parallel tool_calls within ONE step by name. Order
        # preserved by first occurrence so the timeline still reads
        # naturally. Spawns never group — each spawn picks a different
        # child and the focus arg is the only place that info appears.
        groups: list[tuple[str, list[tuple[int, dict]]]] = []
        by_name: dict[str, int] = {}
        for ti, tc in enumerate(tool_calls):
            tname = tc.get("name") or "?"
            if tname == "spawn_agent":
                groups.append((tname, [(ti, tc)]))
                continue
            idx = by_name.get(tname)
            if idx is None:
                by_name[tname] = len(groups)
                groups.append((tname, [(ti, tc)]))
            else:
                groups[idx][1].append((ti, tc))

        for tname, calls in groups:
            if tname == "spawn_agent":
                ti, tc = calls[0]
                args_raw = tc.get("arguments") or ""
                child = _match_spawn(args_raw, children_by_id,
                                     children_in_order, spawn_used)
                if child is not None:
                    cid = child.get("agent_id") or "?"
                    spawn_used.add(cid)
                    child_actor = f"agent:{run_id}:{cid}"
                    out.append(Event(
                        ts=None, kind="agent_spawn",
                        actor=actor_self, target=child_actor,
                        label=_short_focus(args_raw),
                        payload={"arguments": args_raw},
                        session_id=run_id, step=step_num,
                    ))
                    _walk_agent(child, run_id=run_id,
                                parent_actor=actor_self,
                                out=out, run_kind="agent")
                    # Don't synthesise an agent_done here — the
                    # child's own `done(...)` tool call is emitted
                    # AS the return arrow inside the child's walk
                    # (see the `done` branch below). One event per
                    # control-flow handoff, not two.
                continue

            # `done` semantics:
            #   - CHILD agent (spawned): the `done` call IS the
            #     return arrow to the parent. Emit as `agent_done`
            #     event (one event for both perspectives — call
            #     side AND return side of the same control flow
            #     handoff). spawn_agent's matching return is
            #     handled here, NOT via a synthetic emit after the
            #     child walk.
            #   - ROOT agent (no parent_actor): no one to return
            #     to. Render as a self-arrow `agent → agent: done`,
            #     same as reflect — visually "interaction with
            #     self".
            if tname == "done":
                args_raw = calls[0][1].get("arguments") or ""
                try:
                    parsed = json.loads(args_raw or "{}")
                    findings = parsed.get("findings")
                    if isinstance(findings, list):
                        label = f"done({len(findings)} findings)"
                    else:
                        label = "done"
                except Exception:
                    label = "done"
                if parent_actor is not None:
                    out.append(Event(
                        ts=None, kind="agent_done",
                        actor=actor_self, target=parent_actor,
                        label=label,
                        payload={"arguments": args_raw},
                        session_id=run_id, step=step_num,
                    ))
                else:
                    out.append(Event(
                        ts=None, kind="tool_call",
                        actor=actor_self, target=actor_self,
                        label=label,
                        payload={"tool": tname, "arguments": args_raw},
                        session_id=run_id, step=step_num,
                    ))
                continue

            # reflect — "interaction with self". Emit as a self-loop
            # arrow (actor == target). Mermaid draws this as a tiny
            # back-arc on the agent's lane; D2 surfaces it as a
            # participant-self label.
            if tname == "reflect":
                args_raw = calls[0][1].get("arguments") or ""
                out.append(Event(
                    ts=None, kind="tool_call",
                    actor=actor_self, target=actor_self,
                    label=(f"reflect({_short(args_raw, 30)})"
                           if args_raw else "reflect"),
                    payload={"tool": tname, "arguments": args_raw},
                    session_id=run_id, step=step_num,
                ))
                continue

            sys_actor = f"system:{_system_for(tname)}"
            n = len(calls)
            if n == 1:
                args_raw = calls[0][1].get("arguments") or ""
                call_label = f"{tname}({_short(args_raw, 40)})"
            else:
                call_label = f"{tname} ×{n}"
            out.append(Event(
                ts=None, kind="tool_call",
                actor=actor_self, target=sys_actor,
                label=call_label,
                payload={"tool": tname,
                          "arguments": [c[1].get("arguments") or "" for c in calls]},
                session_id=run_id, count=n, step=step_num,
            ))

            paired_results = [tool_results[ti] for ti, _ in calls
                              if ti < len(tool_results)]
            if paired_results:
                res_label = (_short(paired_results[0] or "", 60)
                             if n == 1
                             else f"{len(paired_results)} results")
                out.append(Event(
                    ts=None, kind="tool_result",
                    actor=sys_actor, target=actor_self,
                    label=res_label,
                    payload={"results": paired_results},
                    session_id=run_id, count=len(paired_results),
                    step=step_num,
                ))

        if not tool_calls and resp.get("content"):
            # Text-only step — the LLM produced a final message and
            # exited (mode:single agents, judges, and any ReAct step
            # where the model returned text instead of calling a
            # tool). We treat text-to-human as a tool call to a
            # virtual `system:human` target so the diagram code stays
            # kind-agnostic: same tool_call shape, same renderer path.
            out.append(Event(
                ts=None, kind="tool_call",
                actor=actor_self, target="system:human",
                label=_short(resp["content"], 80),
                payload={"text": resp["content"], "tool": "text"},
                session_id=run_id, step=step_num, count=1,
            ))


# ── Helpers ───────────────────────────────────────────────────────────────


def _run_meta(db_path: str, run_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT started_at, kind, agent_name, scenario_id "
            "FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    return dict(zip(("started_at", "kind", "agent_name", "scenario_id"), row))


def _parse_ts(s: str) -> datetime:
    """Always returns a tz-aware datetime so events from different
    runs can be sorted together (Python forbids comparing aware vs
    naive). If the stored timestamp has no tz, assume UTC."""
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _short_focus(args_raw: str) -> str:
    """Pick `focus=` or `name=` out of spawn_agent args for the
    spawn arrow label."""
    try:
        a = json.loads(args_raw or "{}")
        focus = a.get("focus") or a.get("name") or a.get("agent") or ""
        return f"spawn({_short(focus, 50)})" if focus else "spawn"
    except Exception:
        return "spawn"


def _done_label(output: Any) -> str:
    if isinstance(output, list):
        return f"done({len(output)} findings)"
    if isinstance(output, dict):
        if "verdict" in output:
            return f"done(verdict={output.get('verdict')})"
        return "done(dict)"
    return "done"


def _match_spawn(args_raw: str,
                 by_id: dict[str, dict],
                 in_order: list[dict],
                 used: set[str]) -> Optional[dict]:
    """Match a spawn_agent tool call to one of the children of the
    current agent. Prefer name match (from args) over positional."""
    name = None
    try:
        a = json.loads(args_raw or "{}")
        name = a.get("agent") or a.get("name")
    except Exception:
        pass
    if name:
        for c in in_order:
            if c.get("agent_name") == name and c.get("agent_id") not in used:
                return c
    for c in in_order:
        if c.get("agent_id") not in used:
            return c
    return None


# ── Renderers ─────────────────────────────────────────────────────────────


# ── Role-based coloring ───────────────────────────────────────────────────

# One palette shared across all three renderers so Mermaid / D2 / G6
# colour the same agent the same way. Tuple is (fill, stroke).
# Colours target a dark background; fills are translucent so the
# participant text stays readable.
_ROLE_PALETTE: dict[str, tuple[str, str]] = {
    "reviewer":     ("#1f6feb44", "#58a6ff"),   # blue
    "investigator": ("#a371f744", "#d2a8ff"),   # purple
    "judge":        ("#d2992244", "#e3b341"),   # gold
    "dispatcher":   ("#db6d2844", "#f0883e"),   # orange
    "tools":        ("#56d36444", "#79c0ff"),   # green (systems)
    "agent":        ("#8b949e44", "#c9d1d9"),   # gray fallback
}


def _role_for(uri: str, label: str) -> str:
    """Bucket an actor (URI + label) into one of the palette roles.
    Agents take their `agent_name` (`reviewer`, `investigator`, …);
    systems all share the `tools` lane colour."""
    if uri.startswith("system:"):
        return "tools"
    if not uri.startswith("agent:"):
        return "agent"
    # Label might be `investigator-3` — strip the disambiguator.
    base = label.split("-", 1)[0].strip().lower()
    return base if base in _ROLE_PALETTE else "agent"


def _rgba_for_mermaid(role: str) -> str:
    """Mermaid `box rgb(r, g, b, a)` expects integer rgb + 0..1 alpha.
    Our palette stores `#RRGGBBAA`; convert."""
    fill, _ = _ROLE_PALETTE.get(role, _ROLE_PALETTE["agent"])
    if fill.startswith("#") and len(fill) >= 7:
        r = int(fill[1:3], 16); g = int(fill[3:5], 16); b = int(fill[5:7], 16)
        a = int(fill[7:9], 16) / 255 if len(fill) >= 9 else 1.0
        return f"rgb({r}, {g}, {b}, {a:.2f})"
    return "rgb(128, 128, 128, 0.27)"


_PART_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _safe_id(actor: str) -> str:
    """Diagram-engine-safe identifier for an actor URI.
    `agent:<run_id>:<agent_id>` → `agent_<runprefix>_<aidprefix>`,
    de-duplicated if run_id == agent_id (judge runs collapse the
    two into one short id otherwise — uglier participant name)."""
    parts = actor.split(":")
    if parts[0] == "agent" and len(parts) >= 3 and parts[1] == parts[2]:
        # run_id == agent_id (typical for judge runs): one prefix is enough.
        actor = f"agent:{parts[1]}"
    return _PART_SAFE.sub("_", actor).strip("_") or "X"


def _participants_of(events: list[Event]) -> list[tuple[str, str]]:
    """Stable ordered list of (id, label) for every actor and target
    that appears in events. Agents before systems before anything
    else so the diagram reads left-to-right naturally."""
    order: list[str] = []
    labels: dict[str, str] = {}

    def visit(uri: Optional[str]) -> None:
        if not uri:
            return
        if uri in labels:
            return
        order.append(uri)
        if uri.startswith("agent:"):
            # agent:<run_id>:<agent_id> — label by the first events
            #  that name this actor, fall back to short id.
            short = uri.split(":", 2)[-1][:8]
            labels[uri] = f"agent({short})"
        elif uri.startswith("system:"):
            labels[uri] = uri.split(":", 1)[1]
        elif uri.startswith("human:"):
            labels[uri] = uri.split(":", 1)[1]
        else:
            labels[uri] = uri

    for e in events:
        visit(e.actor)
        visit(e.target)
    # Reorder: agents first, then systems, then everything else.
    agents = [u for u in order if u.startswith("agent:")]
    systems = [u for u in order if u.startswith("system:")]
    others = [u for u in order if u not in agents and u not in systems]
    final = agents + systems + others
    return [(u, labels[u]) for u in final]


def _enrich_agent_labels(events: list[Event],
                         participants: list[tuple[str, str]],
                         db_path: str) -> list[tuple[str, str]]:
    """Replace the placeholder `agent(<short>)` label with the agent's
    real name (reviewer / investigator / judge / dispatcher / …).

    Names are keyed per `(run_id, agent_id)` — subagents share the
    parent's run_id (they're spawned in the same cli.py process), so
    looking up agent_name by run_id alone returns the ROOT agent's
    name for every child. The `events` table is the source of truth
    here: every `agent_started` event carries `(agent_id,
    agent_name)`. One SELECT covers every agent in scope.

    Multiple agents with the same name (3 investigators on one PR)
    get `-N` suffixes in spawn order for disambiguation.
    """
    keys: set[tuple[str, str]] = set()
    run_ids: set[str] = set()
    for u, _ in participants:
        if u.startswith("agent:"):
            parts = u.split(":")
            if len(parts) >= 3:
                run_ids.add(parts[1])
                keys.add((parts[1], parts[2]))
    if not run_ids:
        return participants

    by_id: dict[tuple[str, str], tuple[str, str]] = {}
    conn = sqlite3.connect(db_path)
    try:
        # Pull per-(run_id, agent_id) name from events. Kind comes
        # from runs (judge vs agent — orthogonal to agent_name).
        qmarks = ",".join("?" for _ in run_ids)
        runs_rows = conn.execute(
            f"SELECT id, kind FROM runs WHERE id IN ({qmarks})",
            list(run_ids),
        ).fetchall()
        kind_by_run = {r[0]: (r[1] or "agent") for r in runs_rows}
        # Only `agent_started` events identify their own agent_name.
        # `agent_spawned` writes the parent's agent_id with the child's
        # name; mixing those in poisoned the lookup when an investigator
        # was spawned by a reviewer.
        events_rows = conn.execute(
            f"SELECT DISTINCT run_id, agent_id, agent_name FROM events "
            f"WHERE run_id IN ({qmarks}) AND event_type='agent_started' "
            f"  AND agent_id IS NOT NULL AND agent_name IS NOT NULL "
            f"  AND agent_name != ''",
            list(run_ids),
        ).fetchall()
        for rid, aid, aname in events_rows:
            knd = kind_by_run.get(rid, "agent")
            by_id[(rid, aid)] = (aname, knd)
    finally:
        conn.close()

    relabelled: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    for u, fallback_label in participants:
        if not u.startswith("agent:"):
            relabelled.append((u, fallback_label))
            continue
        parts = u.split(":")
        rid = parts[1] if len(parts) > 1 else ""
        aid = parts[2] if len(parts) > 2 else ""
        aname, knd = by_id.get((rid, aid), ("agent", "agent"))
        # Disambiguate sibling agents with the same name (e.g. four
        # investigators) by suffixing `-N` in spawn order.
        count = used.get(aname, 0) + 1
        used[aname] = count
        if knd == "judge":
            label = f"judge-{count}" if count > 1 else "judge"
        else:
            label = f"{aname}-{count}" if count > 1 else aname
        relabelled.append((u, label))
    return relabelled


# ── Mermaid renderer ──────────────────────────────────────────────────────


_MM_ESCAPE = str.maketrans({
    ":": "·", ";": ",", "<": "‹", ">": "›", '"': "'", "\n": " ",
})


def _mm_escape(s: str) -> str:
    return (s or "").translate(_MM_ESCAPE).strip()


def _step_prefix(e: Event) -> str:
    """Per-agent step number rendered as `[sN] ` prefix. Empty when
    the event has no step (synthesised done arrows, truncation
    sentinels)."""
    return f"[s{e.step}] " if e.step is not None else ""


def to_mermaid(events: list[Event], db_path: str) -> str:
    """Mermaid sequenceDiagram source.

    No global `autonumber` — events from different agents have
    independent step indices, so labelling them with a single
    running counter (1..N across all agents) is misleading. Each
    event carries its per-agent step in the label as `[s<N>]`.

    Budget enforcement is upstream in `events_from_runs` via
    `max_events` — the renderer never truncates on its own."""
    if not events:
        return "sequenceDiagram\n  Note over Empty: no events in scope"

    participants = _participants_of(events)
    participants = _enrich_agent_labels(events, participants, db_path)
    lines: list[str] = ["sequenceDiagram"]

    # Group participants by role so each gets a coloured `box`. Box
    # syntax (Mermaid 9.4+) is the only per-actor colouring mermaid
    # supports out of the box — individual `participant`s aren't
    # styleable inline.
    from collections import OrderedDict
    grouped: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
    for uri, label in participants:
        role = _role_for(uri, label)
        grouped.setdefault(role, []).append((uri, label))

    # Pretty-up the role label (singular if 1, plural with count if more).
    _role_display = {
        "reviewer": "Reviewer", "investigator": "Investigator",
        "judge": "Judge", "dispatcher": "Dispatcher",
        "tools": "Tools", "agent": "Agent",
    }
    for role, items in grouped.items():
        n = len(items)
        display = _role_display.get(role, role.capitalize())
        if n > 1 and role not in ("tools",):
            display = f"{display}s ({n})"
        rgba = _rgba_for_mermaid(role)
        lines.append(f"  box {rgba} {display}")
        for uri, label in items:
            lines.append(f"    participant {_safe_id(uri)} as {_mm_escape(label)}")
        lines.append("  end")

    # Mermaid's `+` / `-` syntax MUST be balanced — emitting a
    # `-->>-` for a participant that was never `->>+`'d trips
    # "Trying to inactivate an inactive participant" and the whole
    # diagram refuses to parse. Track activation state explicitly
    # and downgrade unmatched deactivations to plain arrows.
    activated: set[str] = set()
    for e in events:
        sa, ta = _safe_id(e.actor), _safe_id(e.target or e.actor)
        # If this event is a pair-bundle (multiple tool ops collapsed
        # into one arrow), render the bundle as multi-line via
        # mermaid's `<br/>` line break.
        bundle = (e.payload or {}).get("bundle")
        if bundle:
            # Escape each piece individually so the `<br/>` line-break
            # tag survives — `_mm_escape` strips `<`/`>` (would mangle
            # the tag if we joined first).
            parts = [_mm_escape(p) for p in bundle]
            label = _mm_escape(_step_prefix(e)) + "<br/>".join(parts)
        else:
            label = _mm_escape(_step_prefix(e) + e.label)
        if e.kind == "agent_spawn":
            lines.append(f"  {sa}->>+{ta}: {label}")
            activated.add(ta)
        elif e.kind == "agent_done":
            if sa in activated:
                lines.append(f"  {sa}-->>-{ta}: {label}")
                activated.discard(sa)
            else:
                # Unbalanced (collapsed/truncated stream) — render as
                # a regular dashed arrow so the parser is happy.
                lines.append(f"  {sa}-->>{ta}: {label}")
        elif e.kind == "tool_call":
            lines.append(f"  {sa}->>{ta}: {label}")
        elif e.kind == "tool_result":
            lines.append(f"  {sa}-->>{ta}: {label}")
        else:
            lines.append(f"  Note over {sa}: {e.kind} · {label}")

    # Close any lanes that are still activated (no done arrived —
    # e.g., the run is still running and the agent_done event
    # hasn't fired yet). Without these synthetic close-outs Mermaid
    # warns about open activations at end-of-diagram.
    for aid in list(activated):
        lines.append(f"  deactivate {aid}")

    return "\n".join(lines)


# ── D2 renderer (source-only, savable text) ───────────────────────────────


def to_d2(events: list[Event], db_path: str) -> str:
    """D2 source with `shape: sequence_diagram`. We ship this as text
    only — users copy to play.d2lang.com or `d2` CLI to render. The
    `save → reopen → edit` round-trip is the main reason D2 is here
    (vs Mermaid which is render-only).

    Like `to_mermaid`, the renderer doesn't truncate — budget is
    upstream."""
    if not events:
        return "# empty\nseq: { shape: sequence_diagram }"

    participants = _enrich_agent_labels(events,
                                        _participants_of(events), db_path)

    # Short alias per participant — `r` for reviewer, `i1..N` for
    # investigators, `j1..N` for judges, `diff` / `bb` / etc. for
    # systems. The full `agent_<run>_<aid>` ids appear 80+ times in
    # message arrows on big sessions; swapping to 2-3 char aliases
    # is the difference between fitting under play.d2lang.com's
    # ~8KB URL limit and not.
    _ROLE_PREFIX = {
        "reviewer": "r", "investigator": "i", "judge": "j",
        "dispatcher": "d", "tools": "t", "agent": "a",
    }
    _SYSTEM_ALIAS = {"diff": "diff", "bitbucket": "bb", "control": "ctrl"}
    used_roles: list[str] = []
    seen: set[str] = set()
    actors_meta: list[tuple[str, str, str, str]] = []  # (uri, alias, label, role)
    counts: dict[str, int] = {}
    for uri, label in participants:
        role = _role_for(uri, label)
        if role not in seen:
            seen.add(role)
            used_roles.append(role)
        if uri.startswith("system:"):
            sys_name = uri.split(":", 1)[1]
            alias = _SYSTEM_ALIAS.get(sys_name, sys_name)
        else:
            prefix = _ROLE_PREFIX.get(role, "a")
            counts[role] = counts.get(role, 0) + 1
            alias = f"{prefix}{counts[role]}" if counts[role] > 1 or role != "reviewer" else prefix
        actors_meta.append((uri, alias, label, role))

    # alias lookup: full URI → short alias (used by the arrow loop).
    alias_for: dict[str, str] = {uri: alias for uri, alias, _, _ in actors_meta}

    # `classes:` block defines the palette once; each participant
    # references its class. Compresses style declarations from
    # 5-lines-per-actor to a single `class:` reference.
    lines: list[str] = ["classes: {"]
    for role in used_roles:
        fill, stroke = _ROLE_PALETTE.get(role, _ROLE_PALETTE["agent"])
        fill6 = fill[:7]
        lines.append(
            f'  {role}: {{style: {{fill: {json.dumps(fill6)}; '
            f'stroke: {json.dumps(stroke)}}}}}'
        )
    lines.append("}")
    lines.append("")
    lines.append("seq: {")
    lines.append("  shape: sequence_diagram")
    for _uri, alias, label, role in actors_meta:
        lines.append(
            f'  {alias}: {{label: {json.dumps(label)}; class: {role}}}'
        )

    for e in events:
        sa = alias_for.get(e.actor, _safe_id(e.actor))
        ta = alias_for.get(e.target or e.actor, _safe_id(e.target or e.actor))
        bundle = (e.payload or {}).get("bundle")
        if bundle:
            # Multi-line label: D2 honours newline characters inside
            # quoted strings, json.dumps emits them as `\n` which D2
            # un-escapes when rendering.
            body = "\n".join(bundle)
            label = (_step_prefix(e) + body).replace('"', "'")
        else:
            label = (_step_prefix(e) + e.label).replace('"', "'")
        if sa == ta:
            lines.append(f"  {sa}: {json.dumps(e.kind + ': ' + label)}")
        else:
            lines.append(f"  {sa} -> {ta}: {json.dumps(label)}")
    lines.append("}")
    return "\n".join(lines)


# ── G6 renderer (node-edge, time-agnostic) ────────────────────────────────


def to_g6(events: list[Event], db_path: str) -> dict:
    """AntV G6 expects `{nodes: [...], edges: [...]}`. We aggregate
    by (actor, target) pairs — edge weight is the number of times
    that pair fires (with `count` from collapsed events summed in).
    Loses time order on purpose; useful for a compact "who talks
    to whom" view on big scopes."""
    nodes_by_id: dict[str, dict] = {}
    edges_by_key: dict[tuple[str, str], dict] = {}

    participants = _enrich_agent_labels(events,
                                        _participants_of(events), db_path)
    for uri, label in participants:
        nid = _safe_id(uri)
        kind = (uri.split(":", 1)[0] if ":" in uri else "other")
        role = _role_for(uri, label)
        fill, stroke = _ROLE_PALETTE.get(role, _ROLE_PALETTE["agent"])
        nodes_by_id[nid] = {
            "id": nid, "label": label, "kind": kind, "role": role,
            # `uri` is the canonical actor key (agent:<run>:<aid> or
            # system:<name>). Frontend needs it to feed back into
            # `/api/diagram?actor=…` when the user clicks the node.
            "uri": uri,
            "fill": fill[:7],   # G6 accepts #RRGGBB
            "stroke": stroke,
        }

    for e in events:
        if not e.target or e.actor == e.target:
            continue
        sa, ta = _safe_id(e.actor), _safe_id(e.target)
        # Direction: requests (actor → target). tool_result / done
        # are responses going the other way — fold into the same
        # edge to keep the graph readable.
        if e.kind in ("tool_result", "agent_done"):
            sa, ta = ta, sa
        key = (sa, ta)
        edge = edges_by_key.get(key)
        if edge is None:
            edge = {"source": sa, "target": ta, "count": 0, "kinds": set()}
            edges_by_key[key] = edge
        edge["count"] += max(1, e.count)
        edge["kinds"].add(e.kind)

    edges = []
    for (sa, ta), e in edges_by_key.items():
        edges.append({
            "source": sa, "target": ta,
            "label": str(e["count"]),
            "weight": e["count"],
            "kinds": sorted(e["kinds"]),
        })

    return {
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
    }


# ── Top-level API ─────────────────────────────────────────────────────────


def build_diagram(scope_uri: str, fmt: str, db_path: str,
                  *, max_events: int = 60,
                  actor_filter: Optional[list[str]] = None,
                  edge_filter: Optional[list[tuple[str, str]]] = None):
    """Return (mime_type, body) for the given scope + format.
    Body is a string for mermaid/d2, a dict for g6 (route serialises).

    `max_events` — soft cap. Fair-share progressive collapse fits to
    this budget; pass 0 to disable.

    `actor_filter` / `edge_filter` — surface up the canonical filters
    in `events_from_runs`. Driven by the G6 "click to filter" UX:
    clicking a node fills `actor_filter`, clicking an edge fills
    `edge_filter`, and supplying both narrows to the intersection.
    """
    run_ids = resolve_runs(scope_uri, db_path)
    events = events_from_runs(
        run_ids, db_path,
        max_events=max_events if max_events > 0 else None,
        actor_filter=actor_filter, edge_filter=edge_filter,
    )
    if fmt == "mermaid":
        return ("text/plain; charset=utf-8", to_mermaid(events, db_path))
    if fmt == "d2":
        return ("text/plain; charset=utf-8", to_d2(events, db_path))
    if fmt == "g6":
        return ("application/json", to_g6(events, db_path))
    if fmt == "events":
        # JSON list of canonical Event dicts. Drives the table view
        # in the session trace UI — same `events_from_runs` pipeline
        # as the diagrams, so G6 click-filters and ± budget controls
        # propagate to the table for free.
        return ("application/json",
                _enrich_events_for_ui(events, db_path))
    raise ValueError(f"unsupported format: {fmt}")


def _enrich_events_for_ui(events: list[Event], db_path: str) -> list[dict]:
    """Return events as JSON-safe dicts. We swap raw URIs for the
    same human labels the diagrams use (`reviewer`, `investigator-2`,
    `diff`) so the table reads naturally without a separate
    client-side lookup."""
    if not events:
        return []
    participants = _enrich_agent_labels(
        events, _participants_of(events), db_path,
    )
    label_for: dict[str, str] = {uri: label for uri, label in participants}
    out: list[dict] = []
    for e in events:
        out.append({
            "ts": e.ts.isoformat() if e.ts else None,
            "kind": e.kind,
            "actor": e.actor,
            "target": e.target,
            "actor_label": label_for.get(e.actor, e.actor),
            "target_label": (label_for.get(e.target, e.target)
                             if e.target else None),
            "label": e.label,
            "step": e.step,
            "count": e.count,
            "session_id": e.session_id,
            "bundle": (e.payload or {}).get("bundle"),
        })
    return out


# Test/debug aid — dump events as JSON for inspection.
def events_as_jsonable(events: list[Event]) -> list[dict]:
    out = []
    for e in events:
        d = asdict(e)
        d["ts"] = e.ts.isoformat()
        out.append(d)
    return out
