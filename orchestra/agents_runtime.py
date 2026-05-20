"""Agents-runtime provider — the lifecycle + observation surface for
sub-agents, behind one fakeable interface.

Mirrors the provider pattern the domain layer already uses
(BitbucketPRProvider / JiraProvider / git_repo): a clean protocol
with a real implementation and a fixture-driven fake, so callers —
the agent_spawn / agent_inspect tools, the budget layer, the judge —
depend on the interface, not on `Agent` internals.

What a runtime owns:

  lifecycle    spawn() a sub-agent, await_result()
  observation  list_spawned() — the runs this runtime started;
               status() / trace() / tokens() of any run by id —
               "look at the trace, watch it, price the tokens"
  discovery    list_agents() — the registry as descriptors

Implementations (this module ships the first two; SubprocessRuntime
is Phase 2):

  TraceDBObserver    read-only observation over ~/.diffgraph/traces.db.
                     Works for ANY run id regardless of how it was
                     spawned — it just reads the trace store.
  InProcessRuntime   spawn() delegates to a callback carrying the
                     parent Agent's existing in-process spawn logic;
                     observation is a TraceDBObserver. Behaviour is
                     identical to today — this only puts the existing
                     path behind the interface.
  FakeAgentsRuntime  fixture-driven. spawn() returns canned RunResults
                     keyed by (agent_name, focus-substring); status /
                     trace / tokens are served from the fixture too.
                     The replacement for ToolMocks-of-agent_spawn.

Capture / replay note: because the interface is the only thing
callers touch, a test or a replay harness swaps in FakeAgentsRuntime
and the whole spawn graph becomes deterministic with no real LLM
calls — same move FakeBitbucket makes for PR data.

Compact tool surface. The runtime API below is broad, but the TOOL
surface the LLM sees stays small — 3 tools:

    agent_spawn(agent, focus, sched="sync"|"async", …)  lifecycle
    agent_await(run_id="", timeout=…)                    lifecycle
    agent_inspect(run_id="", view="summary")             discovery + observation

`agent_inspect` is ONE broad tool covering what an agent needs to
SEE about delegation:
  - no arg  → list the runs this runtime started (each: name,
              state, step, tokens) + the registry of agent types
              that can be spawned.
  - run_id  → detail of one run; `view` ∈ summary | trace | tokens
              picks the depth.

WHICH runs an agent may see is an RBAC decision, made by a layer
ABOVE the runtime — not baked in here. The runtime stays neutral:
`list_spawned()` reports what this runtime instance started;
`status` / `trace` / `tokens` resolve any run by id. The RBAC
layer decides which of those a given caller is allowed to read
(e.g. only its own sub-tree, or — for the judge — the one run it
was handed to grade).

`observe()` (live event stream) is a runtime-API method, not a
tool: a streaming iterator doesn't fit the tool-call model.

Async semantics follow TODO §13 (`sched` / `callback` / agent_await
discriminated by completed|partial|interrupted, all riding the
existing pusher pipeline — no new event system).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable


# ── Value objects ────────────────────────────────────────────────────────


@dataclass
class SpawnSpec:
    """Everything needed to start one sub-agent.

    `sched` / `callback` follow TODO §13.2:
      sched="sync"               → spawn() blocks, RunHandle resolves
                                   to a finished run.
      sched="async", callback=T  → spawn() returns immediately; on
                                   completion the result auto-injects
                                   into the parent via the callback
                                   pusher.
      sched="async", callback=F  → returns immediately; caller must
                                   await_result() to retrieve it.
    """
    agent_name: str
    focus: str = ""
    data: dict = field(default_factory=dict)
    context_handoff: str = ""          # "" | handoff-mode name
    sched: str = "sync"                # "sync" | "async"
    callback: bool = True              # async: auto-inject result on done
    parent_run_id: str = ""
    parent_agent_id: str = ""


@dataclass
class RunHandle:
    """Reference to a spawned run. `run_id` keys the trace store;
    `agent_id` keys the in-process child registry."""
    agent_id: str
    run_id: str = ""
    agent_name: str = ""

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "run_id": self.run_id,
                "agent_name": self.agent_name}


@dataclass
class TokenAccounting:
    """Token consumption of a run (summed across its sub-tree)."""
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_paid: int = 0

    def to_dict(self) -> dict:
        return {
            "tokens_in":     self.tokens_in,
            "tokens_out":    self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "tokens_paid":   self.tokens_paid,
        }


@dataclass
class RunStatus:
    """Point-in-time status of a run. `state` ∈ running|completed|
    failed|unknown."""
    run_id: str
    state: str = "unknown"
    agent_name: str = ""
    steps: int = 0
    tokens: TokenAccounting = field(default_factory=TokenAccounting)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id":      self.run_id,
            "state":       self.state,
            "agent_name":  self.agent_name,
            "steps":       self.steps,
            "tokens":      self.tokens.to_dict(),
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class RunResult:
    """Terminal output of a spawned run."""
    agent_id: str
    agent_name: str
    status: str = "completed"          # completed | failed | spawned
    output: Any = None
    sgr_summary: str = ""
    steps: int = 0
    tokens: int = 0
    error: str = ""
    run_id: str = ""

    def to_dict(self) -> dict:
        return {
            "status":      self.status,
            "agent_id":    self.agent_id,
            "agent_name":  self.agent_name,
            "output":      self.output,
            "sgr_summary": self.sgr_summary,
            "steps":       self.steps,
            "tokens":      self.tokens,
            "run_id":      self.run_id,
            **({"error": self.error} if self.error else {}),
        }


@dataclass
class AgentDescriptor:
    """One entry in list_agents() — what a delegate is + can do."""
    name: str
    summary: str = ""
    mode: str = ""
    tools: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "summary":      self.summary,
            "mode":         self.mode,
            "tools":        self.tools,
            "capabilities": self.capabilities,
        }


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class AgentsRuntime(Protocol):
    """The lifecycle + observation contract every runtime satisfies.

    Broad API; the LLM-facing tool surface stays compact (see module
    docstring) — `agent_inspect` folds status/trace/tokens, `observe`
    is API-only.
    """

    # lifecycle
    def spawn(self, spec: SpawnSpec) -> RunHandle: ...
    def await_result(self, handle: RunHandle,
                     timeout: Optional[float] = None) -> RunResult: ...
    # observation — list_spawned() reports this runtime's own runs;
    # status/trace/tokens/observe resolve any run by id. An RBAC
    # layer above decides which a caller may actually read.
    def list_spawned(self) -> list[RunStatus]: ...
    def status(self, run_id: str) -> RunStatus: ...
    def trace(self, run_id: str) -> dict: ...
    def tokens(self, run_id: str) -> TokenAccounting: ...
    def observe(self, run_id: str): ...   # -> Iterator[dict] of events
    # discovery
    def list_agents(self) -> list[AgentDescriptor]: ...


# ── Observation over the trace DB ────────────────────────────────────────


_DEFAULT_DB = Path.home() / ".diffgraph" / "traces.db"


class TraceDBObserver:
    """Read-only observation: status / trace / tokens of any run, by
    id, straight from the SQLite trace store. Decoupled from how the
    run was spawned — it just reads `runs` + `events`. Reused as the
    observation half of InProcessRuntime (and, later, SubprocessRuntime).
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB):
        self.db_path = Path(db_path)

    def _reader(self):
        from .trace_db import TraceDBReader
        return TraceDBReader(self.db_path)

    def status(self, run_id: str) -> RunStatus:
        try:
            reader = self._reader()
        except FileNotFoundError:
            return RunStatus(run_id=run_id, state="unknown")
        try:
            row = reader.conn.execute(
                "SELECT started_at, finished_at, status, agent_name "
                "FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return RunStatus(run_id=run_id, state="unknown")
            tok = self._tokens_from_reader(reader, run_id)
            steps = self._steps_from_reader(reader, run_id)
            # `runs.status` is running|completed|failed (writer contract).
            state = (row["status"] or "unknown").strip() or "unknown"
            return RunStatus(
                run_id=run_id, state=state,
                agent_name=row["agent_name"] or "",
                steps=steps, tokens=tok,
                started_at=row["started_at"] or "",
                finished_at=row["finished_at"] or "",
            )
        finally:
            try: reader.conn.close()
            except Exception: pass

    def trace(self, run_id: str) -> dict:
        try:
            reader = self._reader()
        except FileNotFoundError:
            return {}
        try:
            return reader.get_run_trace(run_id)
        except Exception:
            return {}
        finally:
            try: reader.conn.close()
            except Exception: pass

    def tokens(self, run_id: str) -> TokenAccounting:
        try:
            reader = self._reader()
        except FileNotFoundError:
            return TokenAccounting()
        try:
            return self._tokens_from_reader(reader, run_id)
        finally:
            try: reader.conn.close()
            except Exception: pass

    def observe(self, run_id: str):
        """Yield the run's event dicts in order. Not a live tail —
        for a completed (or in-progress) run it replays what the
        trace store currently holds. A true follow-tail is a
        SubprocessRuntime concern."""
        try:
            reader = self._reader()
        except FileNotFoundError:
            return
        try:
            rows = reader.conn.execute(
                "SELECT agent_id, agent_name, event_type, step, data_json "
                "FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        except Exception:
            rows = []
        finally:
            try: reader.conn.close()
            except Exception: pass
        for r in rows:
            data = {}
            try:
                data = json.loads(r["data_json"]) if r["data_json"] else {}
            except Exception:
                data = {}
            yield {
                "agent_id":   r["agent_id"],
                "agent_name": r["agent_name"],
                "event_type": r["event_type"],
                "step":       r["step"],
                "data":       data,
            }

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _walk_agents(trace: dict):
        """Yield every agent node in the trace tree, depth-first."""
        stack = [trace] if trace else []
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if "agent_id" in node:
                yield node
            stack.extend(node.get("children") or [])

    def _tokens_from_reader(self, reader, run_id: str) -> TokenAccounting:
        trace = {}
        try:
            trace = reader.get_run_trace(run_id)
        except Exception:
            return TokenAccounting()
        acc = TokenAccounting()
        for a in self._walk_agents(trace):
            acc.tokens_in     += int(a.get("tokens_in", 0) or 0)
            acc.tokens_out    += int(a.get("tokens_out", 0) or 0)
            acc.tokens_cached += int(a.get("tokens_cached", 0) or 0)
            acc.tokens_paid   += int(a.get("tokens_paid", 0) or 0)
        return acc

    def _steps_from_reader(self, reader, run_id: str) -> int:
        try:
            trace = reader.get_run_trace(run_id)
        except Exception:
            return 0
        # Root agent's step count is the run's step count.
        for a in self._walk_agents(trace):
            return int(a.get("steps", 0) or 0)
        return 0


# ── In-process runtime ───────────────────────────────────────────────────


class InProcessRuntime:
    """Spawns sub-agents in-process. `spawn_fn` carries the parent
    Agent's existing spawn logic (registry clone, child construction,
    budget debit) so behaviour is byte-identical to today — this class
    only puts that path behind the AgentsRuntime interface.

    `spawn_fn(spec) -> RunResult` does the actual work; the runtime
    adds the interface, the observation half, and discovery.
    """

    def __init__(self, spawn_fn: Callable[[SpawnSpec], RunResult],
                 *, list_agents_fn: Callable[[], list[AgentDescriptor]],
                 db_path: str | Path = _DEFAULT_DB):
        self._spawn_fn = spawn_fn
        self._list_agents_fn = list_agents_fn
        self._observer = TraceDBObserver(db_path)
        # spawn() stashes the RunResult so a later await_result() on a
        # sync handle is a free lookup; async handles resolve via the
        # trace store. `_handles` keeps spawn order for list_spawned().
        self._results: dict[str, RunResult] = {}
        self._handles: list[RunHandle] = []

    def spawn(self, spec: SpawnSpec) -> RunHandle:
        result = self._spawn_fn(spec)
        self._results[result.agent_id] = result
        handle = RunHandle(agent_id=result.agent_id,
                           run_id=result.run_id,
                           agent_name=result.agent_name)
        self._handles.append(handle)
        return handle

    def list_spawned(self) -> list[RunStatus]:
        """Statuses of the runs this runtime instance started. Live
        data from the trace store when a run_id is known; otherwise
        synthesised from the cached RunResult. (Access policy — who
        may read which run — is an RBAC concern handled above.)"""
        out: list[RunStatus] = []
        for h in self._handles:
            if h.run_id:
                st = self._observer.status(h.run_id)
                if st.state != "unknown":
                    out.append(st)
                    continue
            res = self._results.get(h.agent_id)
            if res is not None:
                out.append(RunStatus(
                    run_id=h.run_id or h.agent_id,
                    state=res.status,
                    agent_name=res.agent_name,
                    steps=res.steps,
                    tokens=TokenAccounting(tokens_paid=res.tokens),
                ))
        return out

    def await_result(self, handle: RunHandle,
                     timeout: Optional[float] = None) -> RunResult:
        cached = self._results.get(handle.agent_id)
        if cached is not None:
            return cached
        # No cached result — the run was async; surface what the trace
        # store knows. (Full async await is a Phase 2 concern.)
        st = self._observer.status(handle.run_id or handle.agent_id)
        return RunResult(
            agent_id=handle.agent_id, agent_name=handle.agent_name,
            status=st.state, steps=st.steps, tokens=st.tokens.tokens_paid,
            run_id=handle.run_id,
        )

    def status(self, run_id: str) -> RunStatus:
        return self._observer.status(run_id)

    def trace(self, run_id: str) -> dict:
        return self._observer.trace(run_id)

    def tokens(self, run_id: str) -> TokenAccounting:
        return self._observer.tokens(run_id)

    def observe(self, run_id: str):
        return self._observer.observe(run_id)

    def list_agents(self) -> list[AgentDescriptor]:
        return self._list_agents_fn()


# ── Fake runtime ─────────────────────────────────────────────────────────


class FakeAgentsRuntime:
    """Fixture-driven runtime — deterministic, no real LLM calls.

    Fixture shape:

        agents:                      # discovery — list_agents()
          - name: investigator
            summary: "…"
            tools: [diff_read_file, …]
        spawns:                      # spawn() lookup
          - match:                   # all keys must match the SpawnSpec
              agent_name: investigator
              focus_contains: "cheapest"   # substring test on focus
            result:
              output: {...}          # RunResult.output
              sgr_summary: "…"
              steps: 7
              tokens: 4200
          - match: { agent_name: investigator }   # catch-all (no focus key)
            result: { output: "...", steps: 3, tokens: 900 }
        runs:                        # observation — status()/trace()/tokens()
          <run_id>:
            state: completed
            steps: 7
            tokens: { tokens_paid: 4200, tokens_in: 3800, ... }
            trace: { ... }           # optional pre-baked trace tree

    spawn() picks the FIRST `spawns` entry whose `match` block is
    satisfied. A `result` with no matching entry → a failed RunResult
    naming the unmatched (agent_name, focus) so the test fails loud.
    """

    def __init__(self, fixture: dict):
        self._fixture = fixture or {}
        self._spawn_counter = 0
        self._results: dict[str, RunResult] = {}
        self._handles: list[RunHandle] = []

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FakeAgentsRuntime":
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: agents-runtime fixture must be a mapping")
        return cls(raw)

    # ── lifecycle ────────────────────────────────────────────────────

    def spawn(self, spec: SpawnSpec) -> RunHandle:
        self._spawn_counter += 1
        agent_id = f"fake-{spec.agent_name}-{self._spawn_counter:03d}"
        run_id = f"fakerun-{self._spawn_counter:03d}"
        entry = self._match_spawn(spec)
        res = (entry or {}).get("result", {}) if entry else {}
        self._last = RunResult(
            agent_id=agent_id,
            agent_name=spec.agent_name,
            status="completed" if entry else "failed",
            output=res.get("output"),
            sgr_summary=str(res.get("sgr_summary", "")),
            steps=int(res.get("steps", 0) or 0),
            tokens=int(res.get("tokens", 0) or 0),
            error=("" if entry else
                   f"no fixture spawn entry matched agent={spec.agent_name!r} "
                   f"focus={spec.focus[:80]!r}"),
            run_id=run_id,
        )
        self._results[agent_id] = self._last
        handle = RunHandle(agent_id=agent_id, run_id=run_id,
                           agent_name=spec.agent_name)
        self._handles.append(handle)
        return handle

    def await_result(self, handle: RunHandle,
                     timeout: Optional[float] = None) -> RunResult:
        return self._results.get(
            handle.agent_id,
            RunResult(agent_id=handle.agent_id,
                      agent_name=handle.agent_name, status="failed",
                      error="unknown handle"),
        )

    def list_spawned(self) -> list[RunStatus]:
        """Statuses of the runs started through this fake runtime."""
        out: list[RunStatus] = []
        for h in self._handles:
            res = self._results.get(h.agent_id)
            if res is None:
                continue
            out.append(RunStatus(
                run_id=h.run_id,
                state=res.status,
                agent_name=res.agent_name,
                steps=res.steps,
                tokens=TokenAccounting(tokens_paid=res.tokens),
            ))
        return out

    def _match_spawn(self, spec: SpawnSpec) -> Optional[dict]:
        for entry in (self._fixture.get("spawns") or []):
            m = entry.get("match") or {}
            if m.get("agent_name") and m["agent_name"] != spec.agent_name:
                continue
            fc = m.get("focus_contains")
            if fc and fc not in (spec.focus or ""):
                continue
            return entry
        return None

    # ── observation ──────────────────────────────────────────────────

    def status(self, run_id: str) -> RunStatus:
        r = (self._fixture.get("runs") or {}).get(run_id) or {}
        tok = r.get("tokens") or {}
        return RunStatus(
            run_id=run_id,
            state=str(r.get("state", "unknown")),
            agent_name=str(r.get("agent_name", "")),
            steps=int(r.get("steps", 0) or 0),
            tokens=TokenAccounting(
                tokens_in=int(tok.get("tokens_in", 0) or 0),
                tokens_out=int(tok.get("tokens_out", 0) or 0),
                tokens_cached=int(tok.get("tokens_cached", 0) or 0),
                tokens_paid=int(tok.get("tokens_paid", 0) or 0),
            ),
            started_at=str(r.get("started_at", "")),
            finished_at=str(r.get("finished_at", "")),
        )

    def trace(self, run_id: str) -> dict:
        r = (self._fixture.get("runs") or {}).get(run_id) or {}
        return r.get("trace") or {}

    def tokens(self, run_id: str) -> TokenAccounting:
        return self.status(run_id).tokens

    def observe(self, run_id: str):
        """Replay the fixture's `runs.<run_id>.events` list, if any."""
        r = (self._fixture.get("runs") or {}).get(run_id) or {}
        for ev in (r.get("events") or []):
            yield ev

    # ── discovery ────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentDescriptor]:
        out: list[AgentDescriptor] = []
        for a in (self._fixture.get("agents") or []):
            if not isinstance(a, dict):
                continue
            out.append(AgentDescriptor(
                name=str(a.get("name", "")),
                summary=str(a.get("summary", "")),
                mode=str(a.get("mode", "")),
                tools=list(a.get("tools") or []),
                capabilities=list(a.get("capabilities") or []),
            ))
        return out
