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

| URL | Description |
|---|---|
| `/` | Runs list with search, auto-refresh every 3s |
| `/runs/{id}` | Redirect to live (running) or trace (completed) |
| `/runs/{id}/live` | Real-time event stream with color-coded agents |
| `/runs/{id}/trace` | Split-pane: agent tree left, detail tabs right |

### API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/runs` | List runs as JSON |
| `GET` | `/api/runs/{id}/json` | Full trace data |
| `GET` | `/api/runs/{id}/events` | All events (for live bulk load) |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/messages` | Full messages |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/call` | Tool call args |
| `GET` | `/api/runs/{id}/step/{agent}/{step}/result` | Tool result |
| `GET` | `/api/metrics?hash=X` | Aggregate metrics for prompt hash |
| `GET` | `/api/compare?a=X&b=Y` | Compare two prompt hashes |
| `WS` | `/ws/live/{run_id}` | WebSocket event stream |

### Reverse proxy

Set `TRACE_BASE_PATH` env var when running behind nginx with path prefix:

```bash
TRACE_BASE_PATH=/evo/traces-ui python cli.py serve
```

All template URLs (static, API, WebSocket, links) use the prefix automatically.

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
