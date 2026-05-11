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
    same shape comes from session, scenario_run, plan, pr scopes."""
    ts: datetime
    kind: str               # tool_call | tool_result | agent_spawn |
                            # agent_done | agent_text | judge_verdict
    actor: str              # "agent:<run_id>:<agent_id>" |
                            # "system:<name>" | "human:<slug>" (future)
    target: Optional[str]   # same alphabet as actor
    label: str              # short display string
    payload: dict = field(default_factory=dict)   # full data for click → side panel
    session_id: Optional[str] = None              # which run this belongs to


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
    "react_to_comment":  "bitbucket",
    "set_review_status": "bitbucket",
    # reflect / done / spawn_agent are NOT system calls — they appear
    # as agent_spawn / agent_done / agent_text events directly.
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


def events_from_runs(run_ids: list[str], db_path: str) -> list[Event]:
    """Build the canonical event stream for every run in scope, sort
    by timestamp. Each run's events are extracted independently — the
    transitive closure happened in the resolver."""
    out: list[Event] = []
    for rid in run_ids:
        out.extend(_events_from_run(rid, db_path))
    return sorted(out, key=lambda e: e.ts)


def _events_from_run(run_id: str, db_path: str) -> list[Event]:
    """One run → list of Events. Reuses the prepared tree
    (`orchestra.trace._prepare_agent`) so we don't reinvent pairing
    of tool_calls with tool_results."""
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

    started_at = meta.get("started_at")
    base_ts = _parse_ts(started_at) if started_at else datetime.now(timezone.utc)
    kind = meta.get("kind") or "agent"

    out: list[Event] = []
    _walk_agent(prepared, run_id=run_id, parent_actor=None,
                base_ts=base_ts, out=out, run_kind=kind)
    return out


def _walk_agent(agent: dict, *, run_id: str,
                parent_actor: Optional[str],
                base_ts: datetime,
                out: list[Event],
                run_kind: str) -> None:
    """Recurse the prepared tree; emit Events in step order. Each
    event's ts is `base_ts + step_offset_ms` since per-step
    timestamps aren't on the prepared tree — they're close enough
    for ordering inside one run, and across runs the run-level
    base_ts dominates."""
    aid = agent.get("agent_id") or "?"
    aname = agent.get("agent_name") or "?"
    actor_self = f"agent:{run_id}:{aid}"

    # Special-case judge runs: no tool calls, the verdict lives in
    # `output`. Emit a single judge_verdict event so the diagram
    # picks the judge phase up.
    if run_kind == "judge":
        verdict = agent.get("output") or {}
        if isinstance(verdict, dict):
            score = verdict.get("overall_score")
            v = verdict.get("verdict") or "?"
            label = f"verdict={v}"
            if score is not None:
                label += f" · score={score}"
        else:
            label = "verdict"
        out.append(Event(
            ts=base_ts, kind="judge_verdict",
            actor=actor_self, target=parent_actor,
            label=label, payload={"output": verdict},
            session_id=run_id,
        ))
        return

    # Children indexed by name + id so spawn_agent calls can match.
    children_by_id = {
        (c.get("agent_id") or ""): c for c in (agent.get("children") or [])
    }
    children_in_order = list(agent.get("children") or [])
    spawn_used: set[str] = set()
    step_offset_ms = 0

    for step in (agent.get("paired_steps") or []):
        resp = step.get("resp") or {}
        tool_calls = resp.get("tool_calls") or []
        tool_results = step.get("tool_results") or []

        for ti, tc in enumerate(tool_calls):
            tname = tc.get("name") or "?"
            args_raw = tc.get("arguments") or ""
            step_offset_ms += 50  # synthesise monotonic micro-offsets
            ts = base_ts + _ms(step_offset_ms)

            if tname == "spawn_agent":
                child = _match_spawn(args_raw, children_by_id,
                                     children_in_order, spawn_used)
                if child is not None:
                    cid = child.get("agent_id") or "?"
                    spawn_used.add(cid)
                    child_actor = f"agent:{run_id}:{cid}"
                    out.append(Event(
                        ts=ts, kind="agent_spawn",
                        actor=actor_self, target=child_actor,
                        label=_short_focus(args_raw),
                        payload={"arguments": args_raw},
                        session_id=run_id,
                    ))
                    _walk_agent(child, run_id=run_id,
                                parent_actor=actor_self,
                                base_ts=ts, out=out, run_kind="agent")
                    # Child done — synthesise the return arrow.
                    out_payload = child.get("output")
                    rlabel = _done_label(out_payload)
                    step_offset_ms += 1
                    out.append(Event(
                        ts=ts + _ms(1), kind="agent_done",
                        actor=child_actor, target=actor_self,
                        label=rlabel, payload={"output": out_payload},
                        session_id=run_id,
                    ))
                    continue

            # reflect / done as control events — keep them visible
            if tname in ("reflect", "done"):
                out.append(Event(
                    ts=ts, kind="agent_text",
                    actor=actor_self, target=actor_self,
                    label=tname, payload={"arguments": args_raw},
                    session_id=run_id,
                ))
                continue

            sys_actor = f"system:{_system_for(tname)}"
            out.append(Event(
                ts=ts, kind="tool_call",
                actor=actor_self, target=sys_actor,
                label=f"{tname}({_short(args_raw, 40)})",
                payload={"tool": tname, "arguments": args_raw},
                session_id=run_id,
            ))
            if ti < len(tool_results):
                step_offset_ms += 1
                out.append(Event(
                    ts=ts + _ms(1), kind="tool_result",
                    actor=sys_actor, target=actor_self,
                    label=_short(tool_results[ti] or "", 60),
                    payload={"result": tool_results[ti]},
                    session_id=run_id,
                ))

        # Plain text response — note on the lane.
        if not tool_calls and resp.get("content"):
            step_offset_ms += 50
            out.append(Event(
                ts=base_ts + _ms(step_offset_ms),
                kind="agent_text",
                actor=actor_self, target=actor_self,
                label=_short(resp["content"], 80),
                payload={"content": resp["content"]},
                session_id=run_id,
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


def _ms(n: int):
    from datetime import timedelta
    return timedelta(milliseconds=n)


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


_PART_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _safe_id(actor: str) -> str:
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
    """Replace `agent(<short>)` with the actual agent_name from the
    DB so the diagram has readable lanes (reviewer / investigator /
    judge instead of opaque short ids)."""
    # Map run_id → agent_name (one DB roundtrip).
    run_ids = set()
    for u, _ in participants:
        if u.startswith("agent:"):
            parts = u.split(":")
            if len(parts) >= 3:
                run_ids.add(parts[1])
    if not run_ids:
        return participants
    conn = sqlite3.connect(db_path)
    try:
        qmarks = ",".join("?" for _ in run_ids)
        rows = conn.execute(
            f"SELECT id, agent_name, kind FROM runs WHERE id IN ({qmarks})",
            list(run_ids),
        ).fetchall()
    finally:
        conn.close()
    by_run = {r[0]: (r[1] or "?", r[2] or "agent") for r in rows}

    relabelled: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    for u, _ in participants:
        if u.startswith("agent:"):
            parts = u.split(":")
            rid = parts[1] if len(parts) > 1 else ""
            aname, knd = by_run.get(rid, ("agent", "agent"))
            # Disambiguate multiple agents with the same name (e.g. 3
            # investigators) by suffixing a number.
            count = used.get(aname, 0) + 1
            used[aname] = count
            label = aname if count == 1 and aname not in by_run.values() else f"{aname}-{count}"
            # Special-case judge — label as `judge` regardless of
            # display, more discoverable.
            if knd == "judge":
                label = f"judge-{count}" if count > 1 else "judge"
            relabelled.append((u, label))
        else:
            relabelled.append((u,
                              [lbl for _u, lbl in participants if _u == u][0]))
    return relabelled


# ── Mermaid renderer ──────────────────────────────────────────────────────


_MM_ESCAPE = str.maketrans({
    ":": "·", ";": ",", "<": "‹", ">": "›", '"': "'", "\n": " ",
})


def _mm_escape(s: str) -> str:
    return (s or "").translate(_MM_ESCAPE).strip()


def to_mermaid(events: list[Event], db_path: str, *,
               max_messages: int = 200) -> str:
    """Mermaid sequenceDiagram source. Each tool_call / tool_result /
    spawn / done becomes one arrow line. autonumber so steps are
    labelled 1..N for cross-referencing with the LLM step list on
    the trace page."""
    if not events:
        return "sequenceDiagram\n  Note over Empty: no events in scope"

    participants = _participants_of(events)
    participants = _enrich_agent_labels(events, participants, db_path)
    lines: list[str] = ["sequenceDiagram", "  autonumber"]
    for uri, label in participants:
        lines.append(f"  participant {_safe_id(uri)} as {_mm_escape(label)}")

    emitted = 0
    for e in events:
        if emitted >= max_messages:
            anchor = participants[0][0] if participants else "X"
            lines.append(
                f"  Note over {_safe_id(anchor)}: "
                f"⋯ truncated at {max_messages} events ⋯"
            )
            break
        sa, ta = _safe_id(e.actor), _safe_id(e.target or e.actor)
        label = _mm_escape(e.label)
        if e.kind == "agent_spawn":
            lines.append(f"  {sa}->>+{ta}: {label}")
        elif e.kind == "agent_done":
            lines.append(f"  {sa}-->>-{ta}: {label}")
        elif e.kind == "tool_call":
            lines.append(f"  {sa}->>{ta}: {label}")
        elif e.kind == "tool_result":
            lines.append(f"  {sa}-->>{ta}: {label}")
        elif e.kind == "judge_verdict":
            tgt = ta if e.target else sa
            lines.append(f"  Note over {sa},{tgt}: judge · {label}")
        elif e.kind == "agent_text":
            lines.append(f"  Note over {sa}: {label}")
        else:
            lines.append(f"  Note over {sa}: {e.kind} · {label}")
        emitted += 1

    return "\n".join(lines)


# ── D2 renderer (source-only, savable text) ───────────────────────────────


def to_d2(events: list[Event], db_path: str, *,
          max_messages: int = 200) -> str:
    """D2 source with `shape: sequence_diagram`. We ship this as text
    only — users copy to play.d2lang.com or `d2` CLI to render. The
    `save → reopen → edit` round-trip is the main reason D2 is here
    (vs Mermaid which is render-only)."""
    if not events:
        return "# empty\nseq: { shape: sequence_diagram }"

    participants = _enrich_agent_labels(events,
                                        _participants_of(events), db_path)
    lines: list[str] = ["# diff-graph interaction diagram",
                        "# scope: see /api/diagram endpoint",
                        "",
                        "seq: {",
                        "  shape: sequence_diagram"]
    for uri, label in participants:
        sid = _safe_id(uri)
        lines.append(f"  {sid}: {json.dumps(label)}")

    emitted = 0
    for e in events:
        if emitted >= max_messages:
            lines.append(f"  # truncated at {max_messages} events")
            break
        sa, ta = _safe_id(e.actor), _safe_id(e.target or e.actor)
        label = e.label.replace('"', "'")
        arrow = "->" if e.kind in ("tool_call", "agent_spawn",
                                    "judge_verdict") else "->"
        if sa == ta:
            lines.append(f"  {sa}: {json.dumps(e.kind + ': ' + label)}")
        else:
            lines.append(f"  {sa} {arrow} {ta}: {json.dumps(label)}")
        emitted += 1
    lines.append("}")
    return "\n".join(lines)


# ── G6 renderer (node-edge, time-agnostic) ────────────────────────────────


def to_g6(events: list[Event], db_path: str, *,
          max_messages: int = 1000) -> dict:
    """AntV G6 expects `{nodes: [...], edges: [...]}`. We aggregate
    by (actor, target) pairs — edge weight is the number of times
    that pair fires. Loses time order on purpose; useful for a
    compact "who talks to whom" view on big scopes."""
    nodes_by_id: dict[str, dict] = {}
    edges_by_key: dict[tuple[str, str], dict] = {}

    participants = _enrich_agent_labels(events,
                                        _participants_of(events), db_path)
    for uri, label in participants:
        nid = _safe_id(uri)
        kind = (uri.split(":", 1)[0] if ":" in uri else "other")
        nodes_by_id[nid] = {"id": nid, "label": label, "kind": kind}

    counted = 0
    for e in events:
        if counted >= max_messages:
            break
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
        edge["count"] += 1
        edge["kinds"].add(e.kind)
        counted += 1

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


def build_diagram(scope_uri: str, fmt: str, db_path: str):
    """Return (mime_type, body) for the given scope + format.
    Body is a string for mermaid/d2, a dict for g6 (route serialises).
    """
    run_ids = resolve_runs(scope_uri, db_path)
    events = events_from_runs(run_ids, db_path)
    if fmt == "mermaid":
        return ("text/plain; charset=utf-8", to_mermaid(events, db_path))
    if fmt == "d2":
        return ("text/plain; charset=utf-8", to_d2(events, db_path))
    if fmt == "g6":
        return ("application/json", to_g6(events, db_path))
    raise ValueError(f"unsupported format: {fmt}")


# Test/debug aid — dump events as JSON for inspection.
def events_as_jsonable(events: list[Event]) -> list[dict]:
    out = []
    for e in events:
        d = asdict(e)
        d["ts"] = e.ts.isoformat()
        out.append(d)
    return out
