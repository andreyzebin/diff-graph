# Tracing

CLI, query library, and web server for DiffGraph traces. Everything trace-related in one place.

```
tracing/
  __init__.py
  __main__.py          # CLI: metrics, runs, compare, tag, untag
  query.py             # Query functions + hash prefix resolve
  server/              # FastAPI web server (Alpine.js + Jinja2)
    app.py             # Routes, API, WebSocket live updates
    templates/         # trace.html, live.html, runs.html, macros.html
    static/            # trace.css
  tests/
    test_query.py      # 9 tests
```

## CLI

```bash
# Metrics by prompt hash (prefix match — f79 resolves to full hash)
python -m tracing metrics --hash f79
python -m tracing metrics --hash f7917d60e726e776c08ce6c5537acdc8 --since 2026-04-01
python -m tracing metrics --hash abc123 --format json

# List runs
python -m tracing runs --hash f79
python -m tracing runs --hash abc123 --limit 10 --format json

# Compare two generations
python -m tracing compare --a abc123 --b def456

# Tag/untag runs
python -m tracing tag --run da090cf2 --tag benchmark
python -m tracing tag --run da090cf2 --tag stable
python -m tracing untag --run da090cf2 --tag benchmark

# Custom DB path
python -m tracing metrics --hash abc123 --db /path/to/traces.db
```

Default DB: `~/.diffgraph/traces.db`. Hash prefix resolves automatically (like `git log --abbrev`).

## Commands

### `metrics`

Aggregate metrics for all completed runs with a given prompt hash.

```
$ python -m tracing metrics --hash f79

  prompt_hash: f7917d60e726e776c08ce6c5537acdc8
  runs_count: 12
  findings_avg: 3.5
  tokens_per_finding: 4200.0
  total_tokens_avg: 14700.0
  steps_avg: 44.25
  cache_ratio: 0.908
```

| Metric | Description |
|---|---|
| `runs_count` | Number of completed runs |
| `findings_avg` | Average findings per run |
| `tokens_per_finding` | Total tokens / total findings across all runs |
| `total_tokens_avg` | Average total_tokens_paid per run |
| `steps_avg` | Average LLM steps per run (from events) |
| `cache_ratio` | cached_tokens / prompt_tokens across all events |

### `runs`

List individual runs for a prompt hash.

```
$ python -m tracing runs --hash f79

  2fab7b69-2a5  2026-04-12T20:28  findings=4  tokens=0  completed
  ae0bd23d-8d9  2026-04-12T20:23  findings=3  tokens=0  completed
  df35b855-5f6  2026-04-12T20:16  findings=5  tokens=0  completed
```

### `compare`

Compare two prompt generations side by side with statistical test.

```
$ python -m tracing compare --a abc123 --b def456

  A (abc123): 12 runs
  B (def456): 8 runs

  findings_avg               A=    3.5  B=    2.8  ↓ -20.0%
  tokens_per_finding         A= 4200.0  B= 3800.0  ↓  -9.5%
  total_tokens_avg           A=14700.0  B=10640.0  ↓ -27.6%
  steps_avg                  A=  44.25  B=  38.0   ↓ -14.1%
  cache_ratio                A=  0.908  B=  0.885  ↓  -2.5%

  p-value: 0.0312 (significant)
```

p-value from Mann-Whitney U test on findings_count distributions. Requires `scipy` (optional).

### `tag` / `untag`

Tag runs for filtering (e.g. benchmark, stable, experiment).

```bash
python -m tracing tag --run da090cf2 --tag benchmark
python -m tracing untag --run da090cf2 --tag benchmark
```

Tags stored as comma-separated string in runs table.

## Web server

Trace viewer with split-pane layout, live WebSocket updates, and API.

### Start

```bash
# Standalone
python cli.py serve

# Or via Docker (port 8080)
docker run -p 8080:8080 diffgraph
```

### Pages

**Trace viewer:**

| URL | Description |
|---|---|
| `/` | Runs list with search, auto-refresh every 3s |
| `/runs/{id}` | Redirect to live (running) or trace (completed) |
| `/runs/{id}/live` | Real-time event stream with color-coded agents |
| `/runs/{id}/trace` | Split-pane: agent tree left, detail tabs right |

**Quality API (HTMX + Alpine, shared body via hx-boost):**

| URL | Description |
|---|---|
| `/qa/` | Overview dashboard: run counts, by provider, by scenario |
| `/qa/auto-plan` | Schedule configs (CRUD): tag/scenario filter, cadence, pacing, mode |
| `/qa/plans` | Plan list with state, progress bars, **cancel** for running plans, pagination |
| `/qa/workers` | Worker pool config, fleet status, in-flight tasks |
| `/qa/runs` | Filter chips (kind, model, scenario, mutation, project, tags, …) over all runs |
| `/qa/mutations` | Per-mutation aggregates with hard/soft/methodology axes, ▶ fire on-demand schedules, pagination |

### API endpoints

**Trace viewer:**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/runs` | List runs as JSON (legacy 50-row endpoint) |
| `GET` | `/api/runs/{id}/json` | Full trace data |
| `GET` | `/api/runs/{id}/events` | All events (for live bulk load) |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/messages` | Full messages |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/call` | Tool call args |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/result` | Tool result |
| `GET` | `/api/metrics?hash=X` | Aggregate metrics for prompt hash |
| `GET` | `/api/compare?a=X&b=Y` | Compare two prompt hashes |
| `WS` | `/ws/live/{run_id}` | WebSocket event stream |

**Search / aggregates:**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/search/runs` | Filter runs by kind/model/scenario/mutation/project/tags, paginated |
| `GET` | `/api/search/dimensions` | Distinct values for filter dropdowns (live-refreshed) |
| `GET` | `/api/search/aggregates/by_provider` | Per-model run counts + avg duration |
| `GET` | `/api/search/aggregates/by_scenario` | Per-scenario run counts + avg duration |
| `GET` | `/api/search/aggregates/by_mutation` | Per-mutation aggregate (incl. discovered-but-not-run) |
| `GET` | `/api/search/scoring/{mutation}` | Hard/soft/methodology axes + supporting metrics |
| `GET` | `/api/search/scoring-compare?a=…&b=…` | Side-by-side scoring across two mutations |

**Plans + queue + auto-plan + workers (Quality API):**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/qa/plans` | Create a plan (fan-out across providers × scenarios × repeats) |
| `GET` | `/api/qa/plans` | List plans, paginated, with `meta.total` |
| `POST` | `/api/qa/plans/{id}/cancel` | Soft-cancel a plan (queued tasks → cancelled, in-flight finish) |
| `GET` | `/api/qa/auto-plan/configs` | Schedule configs |
| `POST` | `/api/qa/auto-plan/configs` | Create schedule (tags or explicit scenarios; auto / on_demand) |
| `PUT/PATCH` | `/api/qa/auto-plan/configs/{id}` | Edit / toggle |
| `POST` | `/api/qa/auto-plan/configs/{id}/fire-on` | Fire an on-demand schedule for a chosen mutation |
| `GET` | `/api/qa/worker-pools` / `POST` / `PATCH` | Pool CRUD; supervisor keeps target alive while queue has work |
| `GET` | `/api/qa/workers` | Fleet listing, self-healing (stale heartbeat → dead) |

### Reverse proxy

Set `TRACE_BASE_PATH` env var when running behind nginx with path prefix:

```bash
TRACE_BASE_PATH=/evo/traces-ui python cli.py serve
```

All template URLs (static, API, WebSocket, links) use the prefix automatically.

## Trace data model

Every agent — reviewer, investigator, dispatcher, judge, lead —
writes the same five lifecycle events to `events`:

```
   agent_started          one per run, no step
   agent_llm_request      step N — what was sent to the LLM
   agent_llm_response     step N — content + tool_calls (either
                                    can be empty)
   agent_step             step N — high-level marker
   agent_tool_request /
   agent_tool_response /
   agent_tool_result      step N — only when tool_calls non-empty
   agent_done             one per run, no step
```

`orchestra.Agent._run_single` and `Agent._run_react` go through
the shared `Agent._observe_llm_call(...)` helper, so single-shot
agents (mode:single — judges, lead agents that don't loop) emit
the same step shape as one step of a ReAct loop.

The diagram builder (`server/diagram.py`) walks events into 4
canonical kinds:

```
   tool_call        agent → system:<X>     dispatched a tool
   tool_result      system:<X> → agent     tool returned
   agent_spawn      parent → child         agent_spawn()
   agent_done       child → parent         done() from a child
```

That's it — no `agent_text`, no `judge_verdict`. A text-only step
(LLM returned content but no tool_calls — judges, mode:single
agents) emits a `tool_call` to `system:human` with the text as
label. The renderer doesn't branch on agent kind; everything goes
through the same four-kind path.

### Two step shapes

| Shape | Events | Endpoint behavior |
|---|---|---|
| **TOOL step** | tool_call → tool_result | `/call` returns `{tool_calls: [...]}`; `/result` returns the tool delta |
| **TEXT step** | tool_call (no result) | `/call` returns `{content: "..."}`; `/result` 404s with `"(no tool result for this step)"` |

The `/api/runs/{run_id}/step/{agent_id}/{step}/call` endpoint
returns the assistant message envelope `{content, tool_calls}`
for every step — UI renders whichever the LLM produced. Same
endpoint shape for text-only and tool-bearing steps.

The `/messages` endpoint returns the step's OWN LLM request
(system + user + prior tool history) — that's what `/qa/sessions`
shows in the "history" tab. Works for the last step too, including
final `done` and final text-only steps.

## Python API

```python
from tracing.query import get_metrics, get_runs, compare, tag_run

# Metrics (hash prefix resolves automatically)
m = get_metrics("f79")
print(m.findings_avg, m.tokens_per_finding, m.cache_ratio)

# Runs
runs = get_runs("f79", limit=10)

# Compare
c = compare("abc123", "def456")
for metric, d in c.delta.items():
    print(f"{metric}: {d['diff_pct']:+.1f}%")
print(f"p-value: {c.p_value}")

# Tag
tag_run("da090cf2", "benchmark")
```

## Tests

```bash
pytest tracing/tests/ -v
```

9 tests: metrics aggregation, cache ratio, unknown hash, run listing, limit, comparison, serialization.
