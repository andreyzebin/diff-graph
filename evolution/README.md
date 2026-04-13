# Evolution

Self-sustaining prompt development platform. Manages a population of prompt branches that compete continuously — best branches breed, weakest die, dominant branches merge into the next generation.

## Quick start

```bash
# Initialize main branch from current prompts
python -m evolution init --prompts diffgraph/prompts

# See the population
python -m evolution tree

# Collect metrics from tracing DB
python -m evolution measure --branch main

# Run benchmark suite
export EVOLUTION_BENCHMARK_CMD=".venv/bin/python benchmark/cli.py"
export EVOLUTION_BENCHMARK_CWD="$HOME/repos/code-review-benchmarks"
python -m evolution benchmark --branch main

# Full status (JSON)
python -m evolution status
```

## Commands

### `init` — initialize main branch

```bash
python -m evolution init --prompts diffgraph/prompts
python -m evolution init --prompts /path/to/prompts/v2 --hash abc123
python -m evolution init --prompts bitbucket://server/PROJECT/prompts-repo/refs/main/prompts
```

Computes prompt hash automatically if `--hash` not provided. Sets main as active with 100% traffic.

### `tree` — population visualization

```bash
$ python -m evolution tree

main  100%  fitness=0.665  active
  mut-042-budget ← main  20%  fitness=0.710  active
    mut-042a-tools ← mut-042-budget  5%  fitness=0.720  active
  mut-051-security ← main  15%  fitness=0.690  active
  mut-053-methodology ← main  0%  fitness=0.450  extinct
```

Shows branch hierarchy, traffic allocation, fitness scores, status.

### `measure` — collect metrics

```bash
# Single branch
python -m evolution measure --branch main

# All active branches
python -m evolution measure-all
```

Collects from three sources:
- **Tracing** — runs_count, tokens_per_finding, steps_avg, cache_ratio
- **Analytics** — acceptance_rate, false_positive_rate, feedback_rate (via `dg:` tag)
- **Benchmarks** — benchmark_score, by_capability, regressions

Computes fitness as weighted combination.

### `benchmark` — run benchmark suite

```bash
python -m evolution benchmark --branch main
python -m evolution benchmark --branch mut-042-budget
```

Runs all benchmark scenarios with the branch's prompt URI. Updates benchmark_score and by_capability in measurements.

### `compare` — compare two branches

```bash
$ python -m evolution compare --a main --b mut-042-budget

metric                        A         B      diff
-------------------------------------------------------
fitness                     0.665     0.710    +0.045
benchmark_score             0.820     0.850    +0.030
acceptance_rate             0.710     0.780    +0.070
tokens_per_finding       4200.000  3800.000  -400.000
steps_avg                  44.250    38.000    -6.250
```

### `status` — full population state (JSON)

```bash
python -m evolution status
```

Returns branches, measurements, and config as JSON. Useful for dashboards and automation.

## Environment variables

| Variable | Description | Example |
|---|---|---|
| `EVOLUTION_ANALYTICS_DB` | Path to pr-analytics SQLite DB | `~/repos/pr-analytics/output/bitbucket_cache.db` |
| `EVOLUTION_BENCHMARK_CMD` | Benchmark CLI command | `.venv/bin/python benchmark/cli.py` |
| `EVOLUTION_BENCHMARK_CWD` | Benchmark working directory | `~/repos/code-review-benchmarks` |
| `EVOLUTION_WEBHOOK_URL` | Webhook router URL | `http://localhost:8000` |

## Fitness model

```
fitness = 0.35 × benchmark_score        # deep capability (fast, precise)
        + 0.35 × acceptance_rate         # real-world impact (slow, noisy)
        + 0.20 × efficiency              # 1 - tokens_per_finding/10000
        + 0.10 × feedback_rate            # developer engagement
```

Weights configurable in evolution state (`~/.diffgraph/evolution.json`).

## Branch lifecycle

```
BORN ──── benchmark ────→ BENCHMARKED ──── deploy ────→ ACTIVE
  │         (fail)                                        │
  ▼                                               measure() daily
EXTINCT                                    ┌────────┼────────┐
                                       fitness↑   breed()   fitness↓
                                       sample↑     │        sample↓
                                           │       │            │
                                       DOMINANT  children    EXTINCT
                                           │      BORN
                                    converge (p<0.01, >14d)
                                           │
                                        MERGED (→ new main)
```

## Connectors

| Connector | Source | What it provides |
|---|---|---|
| **TracingConnector** | `~/.diffgraph/traces.db` | runs_count, tokens_per_finding, steps_avg, cache_ratio |
| **AnalyticsConnector** | pr-analytics DB | acceptance_rate, false_positive_rate (via `dg:` tag) |
| **BenchmarkConnector** | benchmark CLI | overall_score, by_capability, regressions |
| **WebhookConnector** | webhook API | route management (deploy/undeploy/rebalance) |

Analytics links to tracing via `dg:gen:hash:run` tag embedded in every Bitbucket comment posted by the agent.

## State

Population state persisted in `~/.diffgraph/evolution.json`. Override with `--state /path/to/state.json`.

Contains: all branches (id, status, fitness, sample%), measurements, config weights.

## Tests

```bash
pytest evolution/tests/ -v
```

9 tests: population lifecycle, branch management, persistence, fitness computation.

## What's next

Evolution core provides the measurement + comparison infrastructure. Coming:
- **MutationAgent** — Orchestra agent that generates prompt mutations driven by benchmark capability scores
- **MergeAgent** — Orchestra agent for semantic prompt merge (combining winning branches)
- **tick()** — automated evolution loop: rebalance (bandit), breed, cull, converge
- **Dashboard** — population tree + capability heatmap + fitness trends
