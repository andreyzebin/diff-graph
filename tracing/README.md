# Tracing

CLI + query library for DiffGraph trace DB. Aggregates efficiency metrics per prompt generation, compares generations, lists runs.

## CLI

```bash
# Aggregate metrics for a prompt hash
python -m tracing metrics --hash f7917d60e726e776c08ce6c5537acdc8

# With date filter
python -m tracing metrics --hash abc123 --since 2026-04-01

# JSON output
python -m tracing metrics --hash abc123 --format json

# List runs for a hash
python -m tracing runs --hash abc123
python -m tracing runs --hash abc123 --limit 10 --format json

# Compare two generations
python -m tracing compare --a abc123 --b def456
python -m tracing compare --a abc123 --b def456 --format json

# Custom DB path
python -m tracing metrics --hash abc123 --db /path/to/traces.db
```

Default DB: `~/.diffgraph/traces.db`

## Commands

### `metrics`

Aggregate metrics for all completed runs with a given prompt hash.

```
$ python -m tracing metrics --hash f7917d60e726e776c08ce6c5537acdc8

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
$ python -m tracing runs --hash f7917d60e726e776c08ce6c5537acdc8

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

p-value from Mann-Whitney U test on findings_count distributions. Requires `scipy` (optional — omitted if not installed).

## API

Trace server also exposes these as HTTP endpoints:

```bash
# Metrics
curl "http://localhost:8080/api/metrics?hash=abc123"

# Compare
curl "http://localhost:8080/api/compare?a=abc123&b=def456"
```

## Python API

```python
from tracing.query import get_metrics, get_runs, compare

m = get_metrics("abc123")
print(m.findings_avg, m.tokens_per_finding, m.cache_ratio)

runs = get_runs("abc123", limit=10)
for r in runs:
    print(r["id"], r["findings_count"])

c = compare("abc123", "def456")
for metric, d in c.delta.items():
    print(f"{metric}: {d['diff_pct']:+.1f}%")
print(f"p-value: {c.p_value}")
```

## Tests

```bash
pytest tracing/tests/ -v
```

9 tests with temporary SQLite DB: metrics aggregation, cache ratio, unknown hash, run listing, comparison, serialization.
