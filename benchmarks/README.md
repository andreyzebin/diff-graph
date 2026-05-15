# Code Review Agent Benchmark

> **In-repo subtree.** This was the standalone `code-review-benchmarks`
> repo; as of the May-2026 monorepo merge it lives inside diff-graph at
> `benchmarks/` (history preserved via `git subtree`). It shares the
> repo's single `.venv` and `.env` — there is no separate setup, no
> `BENCH_REPO_PATH`, no sibling checkout. Install once from the repo
> root (`pip install -r requirements.txt`) and the QA server runs the
> bench from inside its own tree.

Measures and regression-tests the code review agent against Bitbucket
Server. Two tiers of scenarios, sharing one judge:

```
TIER 1 — INTEGRATION (cli.py run)        TIER 2 — UNIT (cli.py run-unit)
─────────────────────────────           ────────────────────────────────
Real Bitbucket Server (auth, API)        Fake provider, local git clone
Full pipeline: dispatch → reviewer →     One agent in isolation
  investigators → judge
30-60 min / pass / N attempts × M        5-30s agent + 5-15s judge per
  providers                                fixture; cheap to fan out
Catches end-to-end issues, latency,      Catches prompt-shape regressions
  multi-provider quirks                    fast, no API quota, no flakes
scenarios/java/* + scenarios/            scenarios/unit/*
  interaction/*

                       │
                       ▼
            shared: runner/judge.py
            (LLMJudge, JudgeOutput, judge prompt)
                       │
                       ▼
            ~/.diffgraph/traces.db
            (kind=agent + kind=judge runs,
             linked via linked_run_id)
                       │
                       ▼
            /qa/scoring trend + boxplot
            (diff-graph trace server UI)
```

Each scenario is a yaml with `expected_output.{required_comments,
concern_focuses,reply,thresholds,assert_via}`. The judge reads
`assert_via` to pick its channel: `pr_comments` (real or fake-PR
sink), `intended_findings` (`done(findings=...)` from
invocations.json), `intended_concerns` (`reflect(...)` +
`agent_spawn(focus=...)` from invocations.json).

For the broader quality-management architecture — how unit tier +
integration tier + production pr-analytics fit together, what
merge_acceptance_rate is, how select-golden bridges prod data into
new bench scenarios — see [`../docs/qa-architecture.md`](../docs/qa-architecture.md).

---

## Setup

The bench has no setup of its own — it rides on diff-graph's:

```bash
# from the repo root
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in BITBUCKET_*, DEEPSEEK_API_KEY, …
cp .llm_creds.toml.example .llm_creds.toml   # LLM provider profiles
cp benchmarks/config.yaml benchmarks/config.local.yaml   # edit overrides
```

`.env`, `.llm_creds.toml`, and `benchmarks/config.local.yaml` are all
gitignored. The bench reads `BITBUCKET_URL / PROJECT / REPO / TOKEN /
AGENT_ACCOUNT` from the shared `.env` (see `.env.example` for the full
list), and `--provider` profiles from `.llm_creds.toml` — the loader
walks up from the cwd, so the repo-root copy is found automatically.
See the [root README](../README.md#llm-provider-profiles) for the
profile format.

### Mirror the example repository

Scenarios target the **FlowMart order service** — a Spring Boot / Gradle
Java project at [`andreyzebin/orderflow`](https://github.com/andreyzebin/orderflow).
Mirror it into your Bitbucket project:

```bash
git clone --mirror https://github.com/andreyzebin/orderflow.git orderflow-mirror
cd orderflow-mirror
git remote add bitbucket https://bitbucket.example.com/scm/myproj/orderflow.git
git push bitbucket --mirror
cd .. && rm -rf orderflow-mirror
```

> One branch per scenario plus `master`. Never merge scenario branches —
> they are permanent fixtures. Re-sync after upstream changes with
> `git push --all --force bitbucket` (history is rewritten when hint
> comments are stripped, so `--force` is required).

---

## Running the benchmark

All commands run from the **repo root** (`benchmarks/` is a package):

```bash
source .env

# Integration tier — full pipeline against real Bitbucket
.venv/bin/python -m benchmarks.cli run --agent-url http://localhost:8080

# Single scenario
.venv/bin/python -m benchmarks.cli run --scenario SCEN-009

# Filter by tag
.venv/bin/python -m benchmarks.cli run --tag security

# Dry-run — no PR, no agent call, just validates YAML
.venv/bin/python -m benchmarks.cli run --dry-run

# Unit tier — one agent in isolation, fake Bitbucket, local clone
.venv/bin/python -m benchmarks.cli run-unit benchmarks/scenarios/unit/reviewer/REV-U-001-store-credit-concerns.yaml --provider=deepseek

# Multi-provider matrix (profiles from ~/repos/.llm_creds.toml)
.venv/bin/python -m benchmarks.cli run -p deepseek -p qwen3-6
.venv/bin/python -m benchmarks.cli run --all-providers

# Reports
.venv/bin/python -m benchmarks.cli report last --html
.venv/bin/python -m benchmarks.cli history

# Benchmark a specific prompt version (evolution A/B)
.venv/bin/python -m benchmarks.cli run --prompts=/path/to/prompts/v2
```

In practice you rarely call `cli.py` by hand — the QA server's worker
pool (`quality_api` / `quality_cli`) leases bench tasks and runs them
via the template in `quality_api/config.py::default_bench_cmd_template`.
Start the server with `./dev_server.sh start` and drive plans from the
`/qa/` UI.

### Trace layout

Set `BENCHMARK_TRACE_DIR` to dump every LLM/tool call to disk:

```
<BENCHMARK_TRACE_DIR>/<YYYYMMDD-HHMMSS[-label]>/
  bench.json      providers, scenarios, agent git_sha, judge model
  summary.json    totals + per-provider rollup + flat rows
  <provider>/<scenario>/attempt-NN/
    agent/        diff-graph trace tree (per-step request+response)
    judge/        judge request.json / response.json
    result.json   verdict + score for this attempt
```

`attempt-NN` auto-increments per `(provider, scenario)`; `BENCH_LABEL`
tags different agent versions into separate sessions.

---

## Scenarios

YAML files under `benchmarks/scenarios/`:

- **`tier:unit`** (`scenarios/unit/`) — one agent in isolation, fake
  Bitbucket + local clone, ~5–15 LLM calls. Drives the pre-commit
  gate. `setup.mocks` short-circuits subagent calls;
  `user_message_from` swaps the agent's task framing without touching
  its system prompt. REV-U-*, INV-U-*, DISP-U-*.
- **`tier:integration`** (`scenarios/java/`, `scenarios/interaction/`)
  — full-stack, no mocks, scored against real PR comments. Drives the
  pre-merge gate.
- **`scenarios/drafts/`** — loader-skipped specs for not-yet-runnable
  scenarios.

`expected_output.assert_via` declares which channel the judge matches;
`concern_focuses` adds keyword groups for reflect-based tests.

### Adding a scenario

1. Push a fixture branch to `orderflow` (`feature/<TICKET>-…` or
   `hotfix/<TICKET>-…`). Never merge it — it's a permanent fixture.
2. Re-sync the Bitbucket mirror.
3. Add `benchmarks/scenarios/<dir>/SCEN-NNN-*.yaml` referencing the
   branch under `input.bitbucket.pull_request.from_branch`.
4. Verify: `.venv/bin/python -m benchmarks.cli run --scenario SCEN-NNN --dry-run`.

---

## Running tests

```bash
# whole merged suite from the repo root
.venv/bin/python -m pytest

# bench tests only
.venv/bin/python -m pytest benchmarks/tests
```

`pytest.ini` at the repo root sets `--import-mode=importlib` so the
engine's `tests/` and the bench's `benchmarks/tests/` don't collide on
the bare `tests` package name; the repo-root `conftest.py` puts
`benchmarks/` on `sys.path` so the bench tests' `from runner.X import`
keeps resolving.

---

## Project structure

```
benchmarks/
├── bitbucket/          # AgentPRView ABC + RealBitbucketFactory (atlassian-python-api)
├── runner/             # scenario loader, agent client, LLM judge, scorer, run / run_unit
├── scenarios/
│   ├── unit/           # tier:unit — one agent in isolation
│   ├── java/           # tier:integration — review scenarios
│   ├── interaction/    # tier:integration — /help, /ask, dispatcher
│   └── drafts/         # loader-skipped, not-yet-runnable
├── fixtures/           # user-messages/ (agent overrides), mocks/ (ToolMocks)
├── prompts/            # reviewer scenario prompts
├── tests/
├── cli.py
└── config.yaml         # committed defaults; config.local.yaml is gitignored
```

See [`AGENTS.md`](AGENTS.md) for the internal architecture (loaders,
factories, judge interface).
