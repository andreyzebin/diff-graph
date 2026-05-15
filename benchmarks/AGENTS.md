# Agent Guide — benchmarks/

This document describes the bench codebase for AI agents and coding
assistants.

> **In-repo subtree.** This was the standalone `code-review-benchmarks`
> repo; since the May-2026 monorepo merge it lives inside diff-graph at
> `benchmarks/`. It shares the repo's `.venv` + `.env`. The package is
> `benchmarks` (was `benchmark`) — invoke as `python -m benchmarks.cli`
> from the repo root. Path constants that used to hardcode the
> diff-graph checkout (`_DIFFGRAPH_REPO_DEFAULT`) now resolve to the
> repo root via `Path(__file__).resolve().parents[2]`.

## What this project does

Benchmarks a code review agent against a set of curated scenarios. Each scenario points at a real branch already published in the test repo (Bitbucket / GitHub). The benchmark creates a PR from that branch, triggers the agent via a configurable strategy, then uses an LLM-as-judge to score whether the agent found the expected issues and made a sound verdict.

**The bench never pushes code.** It only performs metadata operations — create PR, post seed comments, post a trigger comment, fetch back the agent's outputs, decline the PR. All test code (branches with planted bugs, paired step0/step1 branches for incremental review, etc.) must be pre-published in the test repo.

## Repository layout

```
benchmarks/
├── bitbucket/
│   ├── base.py             # AgentPRView ABC, AgentPRViewFactory ABC, data classes
│   ├── real_factory.py     # RealBitbucketFactory + RealBitbucketPRProxy (integration tier)
│   └── __init__.py         # build_proxy(cfg) entry point
├── runner/
│   ├── scenario_loader.py  # load_scenarios(), Scenario dataclass, YAML schema (integration tier)
│   ├── agent_client.py     # AgentClient — HTTP POST /review to the agent under test
│   ├── trigger.py          # Trigger ABC, HttpTrigger, WebhookTrigger (Strategy pattern)
│   ├── judge.py            # LLMClient ABC, Judge ABC, LLMJudge, JudgeOutput (shared)
│   ├── scorer.py           # score_scenario() → ScenarioResult
│   ├── run.py              # run_scenario() — orchestrates one INTEGRATION scenario
│   ├── run_unit.py         # run_unit_fixture() — UNIT-tier subprocess runner +
│   │                       # LLMJudge invocation against FakeBenchPRView
│   ├── fake_view.py        # FakeBenchPRView(AgentPRView) — judge's view of the
│   │                       # in-memory fake-PR sink + payload (no Bitbucket)
│   ├── results_store.py    # SQLite + JSON persistence for run history
│   └── html_report.py      # generates self-contained HTML report from results
├── scenarios/
│   ├── agents/             # tier:unit + tier:integration scenarios, real Bitbucket
│   │   ├── reviewer/       # REV-001-concerns, REV-002, …
│   │   ├── investigator/   # INV-001-cheapest-item, …
│   │   └── dispatcher/     # DISP-001-review-spawn, DISP-002-greeting-cross-thread, …
│   ├── unit/               # tier:unit scenarios, fake Bitbucket, local clone
│   │   ├── reviewer/       # REV-U-001/002/003 (concerns-only)
│   │   ├── investigator/   # INV-U-001 / INV-U-002 (standalone)
│   │   └── dispatcher/     # DISP-U-001 / DISP-U-002 (mocked reviewer)
│   ├── java/               # YAML scenario definitions (review + incremental)
│   ├── interaction/        # /help, /ask, unknown-command, dispatcher scenarios
│   └── drafts/             # Loader-skipped specs for not-yet-runnable scenarios
├── fixtures/
│   ├── user-messages/      # concerns-only.md and similar agent overrides
│   └── mocks/              # ToolMocks fixtures (dispatcher-review-spawn.yaml, …)
├── prompts/
│   └── judge.txt           # LLM judge prompt template
├── tests/
│   ├── test_bitbucket_proxy.py
│   ├── test_judge.py
│   ├── test_run_unit_fake_pr.py        # run_unit fake-PR plumbing
│   └── test_unit_fixture_leak_check.py # leak detection across scenarios/unit/*
├── cli.py                  # Typer CLI: run / run-unit / report / history / ab
├── config.yaml             # Committed defaults (${VAR} placeholders only)
└── config.local.yaml       # Local overrides — gitignored, not committed
```

## Key abstractions

### `AgentPRView` (`bitbucket/base.py`)

Verification-only view of a single benchmark PR. Created by a factory, used as an async context manager.

```python
proxy.pr_id                       # int — Bitbucket PR id
await proxy.get_comments()        # list[CommentThread] — agent account only
await proxy.get_review_status()   # ReviewStatus | None — agent account only
await proxy.add_reviewer(username)# adds a reviewer (used by WebhookTrigger)
await proxy.close()               # declines the PR, called automatically by __aexit__
```

`get_comments()` uses the **activities** endpoint filtered by `action == "COMMENTED"`.
`get_review_status()` reads the participants endpoint and filters by agent slug.
Both methods return only activity from the configured `agent_account`.

### `AgentPRViewFactory` (`bitbucket/base.py`)

```python
proxy = await RealBitbucketFactory.build(cfg)
```

`cfg` must contain `provider`, `connection` (from config), and `pull_request` (from scenario).
`build_proxy(cfg)` in `__init__.py` dispatches on `cfg["provider"]`.

The factory also applies SSL config from `connection.ssl`:
- `ca_cert` — overrides session verify with a custom CA bundle
- `client_cert` / `client_key` — mutual TLS client certificate (PEM)

### `Trigger` (`runner/trigger.py`)

Strategy for activating the agent under test. Selected from `config.yaml` via `agent.trigger`.

```python
class HttpTrigger(Trigger):
    # POSTs to agent's /review endpoint with pr_id
    async def activate(proxy): await agent_client.run(pr_id=proxy.pr_id)

class WebhookTrigger(Trigger):
    # Adds agent_account as PR reviewer, then sleeps timeout_seconds
    async def activate(proxy): await proxy.add_reviewer(agent_account); sleep(timeout)
```

Use `http` when the agent has a direct HTTP endpoint.
Use `webhook` when the agent is already wired to Bitbucket via `PR_REVIEWER_UPDATED` webhook.

### `LLMJudge` (`runner/judge.py`)

`AgentPRView` is injected at construction time. `evaluate(scenario)` fetches comments and
review status from the view internally — callers do not pass them.

```python
judge = LLMJudge(llm_client, proxy)
output: JudgeOutput = await judge.evaluate(scenario)
# output.comments, output.review_status, output.score, output.summary
```

Two LLM clients available:
- `AnthropicLLMClient` — uses `ANTHROPIC_API_KEY`
- `OpenAILLMClient(model, api_url, api_key)` — any OpenAI-compatible endpoint

Selected by config: if `judge.api_url` is set → `OpenAILLMClient`; otherwise `AnthropicLLMClient`.

The judge code path is shared between integration and unit tiers.
For the unit tier the `AgentPRView` is `FakeBenchPRView`
(`runner/fake_view.py`) — same interface, reads from the
fake-bitbucket sink + payload instead of HTTP-ing a real
Bitbucket. Switching tiers does not touch judge code.

### `FakeBenchPRView` (`runner/fake_view.py`)

Unit-tier view. Constructed after the agent subprocess finishes —
takes `sink_records` (parsed JSONL the agent's pr_post_comment /
set_status writes landed in) and the original `payload` (repo_path,
base/source SHAs, seed comments, metadata) and exposes them as
the four `AgentPRView` reads the judge needs:

- `get_comments()` → `CommentThread[]` synthesised from the four
  sink kinds (`pr_post_comment`, `post_general`, `review_comment`,
  `reply`) — reactions / resolves / verdict events are not comments
- `get_all_comments()` → seed comments from `pr_state.comments`
  + the agent's posts (judge's full-thread reply path uses this)
- `get_review_status(verdict_source)` → last `set_status` sink
  record, or scans general comments for `[verdict:STATUS]` markers
  when `verdict_source` is `"comment"` / `"both"`
- `get_diff()` → `git diff base..source` against the temp clone
- `get_raw_file(path, ref)` → `git show ref:path`

No network. All methods are async to match the ABC.

### `run_unit_fixture` (`runner/run_unit.py`)

UNIT-tier orchestrator — equivalent of `run_scenario` for the
fake-bitbucket path:

1. Loads a unit yaml via `load_fixture()`
2. `git clone --local --no-hardlinks` the source repo into a
   tempdir, checks out source_branch, resolves (base, source) SHAs
3. Writes the fake-PR payload to a temp JSON; sink to a temp
   JSONL; passes both paths via env to diff-graph's cli.py
4. Spawns `cli.py run --agent={agent} --pr-url=fake://… --message
   … --mocks=… --user-message-from=… --invocations-out=…`
5. After the subprocess exits: reads sink JSONL, force-closes the
   agent's runs row if the subprocess died abnormally
6. If `expected_output` is set and `judge_cfg` is supplied: builds
   `FakeBenchPRView` + a `Scenario` dataclass via
   `_build_scenario_from_unit_fixture` and calls
   `LLMJudge.evaluate()` with `_finish_trace()` in a finally block

OTel covered on both sides — agent via `DIFFGRAPH_TRACE_PATH=
agent_dir`, judge via `LLMJudge.judge_dir → JudgeTraceWriter`.
Both write a `runs` row to `~/.diffgraph/traces.db` and a
filesystem trace tree under `attempt_dir/runs/{agent,judge}/`,
linked via `linked_run_id`.

### `run_scenario` (`runner/run.py`)

```
build_proxy(cfg)            # create PR in Bitbucket
  └─ trigger.activate(proxy)  # HttpTrigger: POST /review  |  WebhookTrigger: add reviewer + sleep
  └─ judge.evaluate(scenario) # fetch comments, score with LLM
  └─ score_scenario(...)      # → ScenarioResult
  └─ proxy.close()            # decline the PR (via __aexit__)
```

## Configuration

`config.yaml` contains committed defaults with `${VAR}` placeholders.
`config.local.yaml` (gitignored) is deep-merged on top at runtime — write only the keys you want to override.

```yaml
# config.local.yaml example
bitbucket:
  connection:
    base_url: "https://bitbucket.mycompany.com"
    project: "MYPROJ"
    repo: "orderflow"
    agent_account: "review-bot"
    ssl:
      ca_cert: "/path/to/ca.crt"
      client_cert: "/path/to/client.pem"

agent:
  trigger: "webhook"       # http | webhook
  timeout_seconds: 120

judge:
  model: "deepseek-chat"
  api_url: "https://api.deepseek.com/v1"
  api_key: "${DEEPSEEK_API_KEY}"
```

Secrets go in `.env` (gitignored), sourced before running:

```bash
source .env
python -m benchmarks.cli run --scenario SCEN-009
```

## Scenario YAML format

```yaml
id: SCEN-NNN
name: "Human-readable name"
tags: [java, security, severity:critical]

input:
  bitbucket:
    provider: real
    pull_request:
      from_branch: "feature/PROJ-NNN-short-name"
      to_branch: "master"
      title: "[BENCHMARK] SCEN-NNN: Short description"
      description: |
        ## Jira ticket content — the agent reads this

expected_output:
  required_comments:
    - id: EXP-1
      type: inline          # inline | general
      severity: critical    # critical | major | minor
      location:
        file: "src/Foo.java"
        line: 42
      description_keywords:
        - ["keyword1", "alt1"]   # row = OR, rows = AND
      rationale: "Why the agent must raise this"
  forbidden_comments:
    - description: "Topic the agent must not raise"
  expected_status_change: "NEEDS_WORK"  # NEEDS_WORK | APPROVED
  thresholds:
    min_score: 0.70
    min_required_found: 1
    max_false_positives: 3

metadata:
  difficulty: medium        # easy | medium | hard
  language: java
  pr_size: small            # small | medium | large
  scenario_type: bug        # bug | security | design | performance | style | test_coverage
```

`description_keywords` logic: each row is a list of alternatives (OR). All rows must match (AND).
Matching is semantic, not literal substring.

### Unit fixture format (`scenarios/unit/*.yaml`)

Different top-level shape — drives `run_unit.py`, not the
integration `Scenario` loader. The judge reads the same
`expected_output` block at the bottom either way; only the
inputs differ.

```yaml
id: REV-U-001-store-credit-concerns
agent: reviewer                       # which agent cli.py runs
tags: [tier:unit, agent:reviewer, isolation, concerns-only]
bench_cmd: >                          # how plans fire this fixture
  cd {bench_root} && source .env
  && unset ALL_PROXY all_proxy
  && .venv/bin/python -m benchmarks.cli run-unit
  {fixture_path} --provider={provider}
repo:
  source: /home/andrey/repos/code-review-examples/orderflow
  base_branch: master
  source_branch: feature/ORD-301-store-credit
user_message_from: ../../../fixtures/user-messages/concerns-only.md
mocks: ../../../fixtures/mocks/dispatcher-review-spawn.yaml  # optional
pr_state:
  metadata:
    title: "…"
    description: |
      …
    pr_url: "fake://orderflow/UNIT/repos/orderflow/pull-requests/301"
    bot_user: "diffgraph-bot"
  comments: []                        # seed threads, visible to pr_list_threads
trigger:
  type: comment
  text: "/review"
  comment_id: 0
agent_data:                           # interpolated into {placeholders}
  focus: |
    …
expected_output:                      # same shape as integration
  assert_via: [intended_concerns]
  concern_focuses: [...]              # reviewer reflect rubric
  required_comments: [...]            # investigator done rubric
  reply: {must_mention: ..., forbidden_topics: ...}  # dispatcher
  side_effects: {inline_comments: 0, review_status_change: false}
  thresholds: {min_score: 0.5, ...}
leak_allowlist: [...]                 # whitelisted scope identifiers
metadata: {difficulty, language, ...}
```

Loader: `runner/run_unit.py:load_fixture` (returns `UnitFixture`).
Conversion to `Scenario` for the judge:
`runner/run_unit.py:_build_scenario_from_unit_fixture`.

Two static guards keep this shape leak-free:

- `tests/test_unit_fixture_leak_check.py` — no expected_output
  keyword may appear verbatim in the agent inputs (user_message,
  agent_data.*, pr_state.metadata, trigger.text, seed comments).
  Generic vocabulary is allowlisted globally; structurally-
  unavoidable overlaps go in per-fixture `leak_allowlist: [...]`
  with a rationale comment.
- `diff-graph/tests/test_prompts_no_fixture_leak.py` — production
  agent prompts cannot contain fixture-specific code identifiers.

## CLI reference

```bash
# Integration tier (real Bitbucket)
python -m benchmarks.cli run --agent-url http://localhost:8080
python -m benchmarks.cli run --scenario SCEN-009
python -m benchmarks.cli run --tag security
python -m benchmarks.cli run --dry-run
python -m benchmarks.cli run --no-verify-ssl          # skip TLS cert verification
python -m benchmarks.cli run --compare-with last
python -m benchmarks.cli ab --agent-a http://v1:8080 --agent-b http://v2:8080
python -m benchmarks.cli report last                  # terminal table
python -m benchmarks.cli report last --html           # HTML file, opens in browser
python -m benchmarks.cli history

# Unit tier (fake Bitbucket, local clone)
python -m benchmarks.cli run-unit benchmarks/scenarios/unit/reviewer/REV-U-001-store-credit-concerns.yaml \
    --provider deepseek
python -m benchmarks.cli run-unit ...path... --no-judge    # agent-only smoke
python -m benchmarks.cli run-unit ...path... --attempt-dir=/tmp/manual  # explicit trace dir
```

The `--attempt-dir` is auto-derived from `BENCHMARK_TRACE_DIR`
when fired by the quality-api worker (plan-fire flow), so plans
get judge scores for free.

## Testing

```bash
# from the repo root
.venv/bin/python -m pytest benchmarks/tests
```

Tests use `CapturingLLMClient` (records prompt) and `MockProxy(AgentPRView)` (returns fixed
comments/status). No real Bitbucket or LLM calls are made.

## Adding a new scenario

1. Push a feature branch to the target repo containing the bug/issue to detect
2. Create `benchmarks/scenarios/java/SCEN-NNN-short-name.yaml` following the format above
3. Verify: `python -m benchmarks.cli run --dry-run --scenario SCEN-NNN`
4. Run: `python -m benchmarks.cli run --scenario SCEN-NNN`

### Test-code authoring rules

The agent reads the diff, comments, AGENTS.md of the test repo, and
arbitrary source files at will. Anything visible to the agent is fair
game for it to use as evidence. So:

- **Never** mention the scenario or its expected output in code,
  commit message, comments, javadoc, or PR description. A line like
  `// BUG: missing null guard` or `// intentional N+1 for SCEN-304`
  hands the answer to the agent and invalidates the test.
- The PR description should read like a real ticket: ID, motivation,
  acceptance criteria. No mentions of "we are testing X" or "this PR
  is part of a benchmark scenario".
- Planted bugs must be visible only through code reading (a missing
  annotation, a wrong-index `get(0)`, a per-iteration repository call).
  No marker comments, no hint variable names like `buggyHelper`.
- "Clean" PRs (false-positive resistance scenarios) must be genuinely
  clean. If the agent finds a real inaccuracy in the diff that you
  intended to be no-bug, the bug is yours — fix the test code, don't
  forbid the finding in `forbidden_comments`.

### Drafts

Scenarios that need bench-side machinery that doesn't exist yet
(BranchUpdater, multi-repo connection_override, etc.) live under
`scenarios/drafts/`. The loader skips that directory entirely so a
draft never accidentally runs and fails on missing branches. Each
draft carries a `metadata.status` field and a "Bench-side TODO" block
listing what needs to land before promotion.

### Cost tags

Scenarios tagged `cost:expensive` use spawn_many with several
investigators, two-round flows, large diffs, or extra repos — they
dominate wall-clock and token cost on a matrix run. Useful filters:

```bash
python -m benchmarks.cli run                              # all 11 scenarios
python -m benchmarks.cli run -t cost:expensive            # only the heavy ones
# (no native exclude flag; for "everything except expensive" use a
#  compound tag like 'java' that the heavy ones don't carry, or
#  --scenario for explicit lists)
```

## Aggregation across attempts

LLM judges and qwen-class agents both have non-trivial variance.
A single run can land 0.55 vs 0.75 on the same scenario back-to-back.

```bash
python -m benchmarks.cli run --repeat 3 -p qwen3-6
```

Each scenario runs N times, each attempt gets its own
`attempt-NN/result.json`, and the bench summary line shows the median
plus the `[min..max]` window. Verdict aggregation: pass when at least
half the attempts passed; error only when every attempt errored.
Warnings (scenario_warnings + agent_warnings) are unioned across
attempts, deduped by `(kind, detail)` so a flaky judge that emits a
warning on 2 of 5 attempts still surfaces it.

## Judge output streams

Three independent signals from the judge per scenario:

- **score / required_comments / false_positives** — the binary scorecard.
  Score and verdict feed the pass/fail call.
- **scenario_warnings** — flags about the *test design itself*: leaky
  description, unfulfillable expectation, contradiction with seed
  comments, trigger mismatch, other. Critique the scenario, not the
  agent.
- **agent_warnings** — flags about the *agent's reasoning quality*,
  independent of scoring: wrong-location, wrong-reasoning,
  surface-acceptance, contradicts-codebase, methodology-gap,
  interface-violation (no `[bot_user]` prefix or no dg footer), other.

`agent_warnings` does not affect overall_score — it surfaces the kinds
of concerns a binary scorecard misses. Two prompts that both score 0.7
can produce very different `agent_warnings` shapes; that's the
discriminator for prompt-comparison work.

The judge is fed the actual PR diff and AGENTS.md (when available) so
its `wrong-location` / `contradicts-codebase` / `methodology-gap`
calls are grounded in code, not asserted from world knowledge alone.
Both blobs are size-capped (~30k diff, ~10k AGENTS.md) before being
substituted into the prompt.

Reasonable findings the agent posts that aren't in `expected_output`
are NOT a quality regression — they're just out-of-scope-for-the-test.
The default agent strictness policy ("APPROVED unless a BLOCKER/MAJOR
stands") absorbs this gracefully.
