# Quality management architecture — how we keep improving the agent

A working code-review agent isn't a one-shot deliverable, it's a
moving target: every prompt edit, every new tool, every LLM provider
update can drift behavior in invisible ways. This doc describes
how we keep that drift visible and reversible — the closed loop
that turns "did this change make the agent better?" from a feeling
into a number.

Updated: 2026-05-12.

## The keystone metric — production merge acceptance

The strongest signal we have about agent quality lives in
**pr-analytics' `merge_acceptance_rate`**: for each inline comment
the agent posts on a production PR, an LLM judge fetches the diff
the human author actually merged and decides whether the agent's
point was addressed (YES / PARTIAL / NO). The rate is

```
   merge_acceptance_rate = (YES + 0.5 × PARTIAL) / (YES + PARTIAL + NO)
```

Why it's strongest:
- it measures **outcome** — code that shipped, not opinions about
  the comment in isolation
- the judge has the full diff in hand, so "comment said X, code did Y" is decidable rather than estimated
- it's continuously collected on every production PR, so the sample
  size grows organically without bench costs

Everything below is in service of moving this number up without
regressing it. Bench scores are PROXIES — they exist to predict
merge_acceptance_rate, not replace it.

## The three loops

Three measurement loops, each running at a different cadence and
asking a different question:

```
                       ┌─ pr-analytics select-golden ──┐
                       │  (weekly, GOLD → new SCEN)    │
                       ▼                               │
   ┌────────────┐  ┌────────────────┐  ┌───────────────┴───┐
   │ UNIT tier  │→ │ INTEGRATION    │→ │ PRODUCTION         │
   │ minutes    │  │ 30-60 min      │  │ continuous         │
   │ /qa/scoring│  │ /qa/scoring    │  │ pr-analytics       │
   │ "did this  │  │ "does the full │  │ "did the merged    │
   │  agent's   │  │  pipeline      │  │  diff actually     │
   │  prompt    │  │  still work    │  │  address what the  │
   │  regress?" │  │  end-to-end?"  │  │  agent said?"      │
   └────────────┘  └────────────────┘  └────────────────────┘
       │                   │                   │
       └───── faster ──────┴───── slower ──────┘
            cheaper          ground truth
```

Signals propagate **right-to-left**: production gaps drive bench
scenarios, bench regressions block deploys, unit-tier regressions
block merges.

### Loop 1 — UNIT (per commit, per fixture)

Each fixture exercises one agent against a known-good rubric. No
spawn fan-out, no real Bitbucket. Score is judge verdict on
`reflect(...)` / `done(findings=...)` / fake-PR sink — whichever
channel that agent uses.

Lives in `benchmarks/scenarios/unit/*`
(yaml). Runner is `bench run-unit`. Fake provider is
`diff-graph/diffgraph/bitbucket_fake.py` (class-based, isolated per
test). After the agent subprocess exits, run_unit builds a
`FakeBenchPRView` over the captured sink + payload and invokes
`LLMJudge.evaluate(scenario)` — same judge code the integration
tier uses, no parallel codebase.

**Question it answers.** "Did this prompt edit break the agent's
reasoning shape on a curated case?"

**Cadence.** Every commit (smoke), plus on-demand via /qa/plans.
~5-30s agent + 5-15s judge per fixture; n=5 batch under 5 min.

**What we DON'T trust unit-tier scores to mean.** That the agent
is good in production. That a 0.85 fixture passes a 0.80 baseline
threshold means anything beyond "this fixture didn't regress in
isolation". Unit-tier scores are FLOOR signals — a regression
means something broke, but holding the score doesn't mean the
deployed agent is good.

### Loop 2 — INTEGRATION (nightly + on merge candidate)

Same scenarios run through the full pipeline against real
Bitbucket PRs — actual pr_post_comment / agent_spawn /
set_review_status. Catches everything the unit tier hides: parallel
spawn ordering quirks, mocked-vs-real Bitbucket API divergences,
end-to-end latency, multi-provider retry behavior.

Lives in `scenarios/agents/*` + `scenarios/java/*` +
`scenarios/interaction/*`. Runner is `bench run`.

**Question it answers.** "Does the agent still produce good output
when nothing is stubbed?"

**Cadence.** Nightly + before each release candidate merge. 30-60
min per pass. Same providers as production.

### Loop 3 — PRODUCTION (continuous via pr-analytics)

Production loop is in a separate repo
(`/home/andrey/repos/pr-analytics`) with its own SQLite cache of
Bitbucket activity. Three metrics matter for agent quality:

| Metric | What it measures | LLM judge? |
|---|---|---|
| `merge_acceptance_rate` | did the merged diff address the agent's inline comment | yes — `analyze-merges`, prompt `judge_merge_acceptance.txt` |
| `feedback_acceptance_rate` | did the next non-bot comment / commit address the agent's claim | yes — `analyze-feedback` |
| `cycle_time`, `throughput`, `acceptance_rate` | PR open→merge, PRs/period, merged% | no — pure SQL |

The first two are continuous on every merged PR; the third are
collected automatically as the cache backfills.

**Question it answers.** "When the agent leaves a comment in
production, does anything good happen because of it?"

**Cadence.** `analyze-merges` and `analyze-feedback` runs on a
weekly cron over the previous 7-14 days. Charts surface as trends
via `pa trend --metrics merge_acceptance_rate,feedback_acceptance_rate
--period biweek`.

## The improvement loop

How a prod observation becomes a bench scenario becomes a fix:

```
   1. pa trend ────────► merge_acceptance_rate dipping last 2 weeks
                              │
                              ▼
   2. pa select-golden ── classify which comments scored NO
      (drilldown)               │
                              ▼
   3. pick 2-3 PRs ──────► copy diff + human comment thread into
      where the agent          a SCEN-NNN.yaml (integration) +
      missed                   <AGENT>-U-NNN.yaml (unit mirror)
                              │
                              ▼
   4. fire new SCEN ─────► see agent baseline on these — likely
      via /qa/plans            fails or scores low (otherwise
                              why is prod missing them?)
                              │
                              ▼
   5. iterate on prompt ─► /qa/scoring trend chart shows whether
      / tool surface           each prompt edit improves the new
                              scenarios WITHOUT regressing
                              existing ones
                              │
                              ▼
   6. ship & watch ──────► next pa trend window shows whether
                              merge_acceptance_rate recovered
```

Steps 1-2 weekly (cheap, mostly SQL + cached LLM judgements).
Steps 3-4 per session when a regression surfaces. Step 5 is the
core inner loop — many prompt edits, fast bench feedback, no
production exposure. Step 6 closes the loop weeks later when prod
data accumulates.

### Why `select-golden` is the bridge

`pa select-golden` runs a five-phase pipeline over recent merged
PRs:

1. **heuristic** — fast SQL filter (lifetime, reviewer count,
   comment count) to a candidate set of ~50
2. **classify** — LLM tags each comment with type (СТИЛЬ /
   ГЛУБОКАЯ_ЛОГИКА / АРХИТЕКТУРА / БЕЗОПАСНОСТЬ / БИЗНЕС_ЛОГИКА /
   ТЕСТЫ / …) and depth
3. **analyze** — `feedback_acceptance_rate` judge on un-analyzed
   comments
4. **score** — composite per-PR score (deep-vs-surface ratio,
   acceptance, …)
5. **judge** — final verdict GOLD / SILVER / REJECT

GOLD PRs are what we mine for new bench scenarios. Two things make
them valuable:
- they're **production-real**: the diff existed in the wild, the
  human reviewer's accepted comments are ground-truth concerns
- they're **classified**: we know which axes (security / arch /
  business logic / …) are under-covered in the bench by looking
  at the GOLD distribution vs. the existing scenario tag mix

## What "stable improvement" requires

Three guarantees the architecture has to give, otherwise
improvement is one-step-forward-two-back:

### A — leak-free fixtures

If a fixture pre-tells the agent the answer, the score is
circular: prompt says "look at X", agent reflects on X, judge
scores "found X". Two static guards catch this:

- `benchmark/tests/test_unit_fixture_leak_check.py` — no
  expected_output keyword may appear in the fixture's own input
  (user_message_from / agent_data.* / pr_state metadata / trigger
  / seed comments). Per-fixture `leak_allowlist: [...]` for
  legitimately unavoidable overlaps.
- `diff-graph/tests/test_prompts_no_fixture_leak.py` — production
  agent prompts (`diffgraph/prompts/*.md` except `judges/`) can't
  contain code identifiers (CamelCase / parens / @ / ALLCAPS) that
  any bench fixture grades on. Auto-derives the forbidden list
  from the bench yamls — new fixtures auto-extend coverage.

Both run on every pytest pass. Both caught real leaks during
their first runs (the May-2026 cleanup pass — 4 leaks in
production prompts + 3 in fixtures).

### B — closed-loop feedback metric

The unit tier's score has to predict (or at least correlate with)
`merge_acceptance_rate`. We do NOT have this calibrated yet —
it's a TODO. The minimum is to plot:

```
   agent prompt SHA → unit-tier mean score (last 7 days) →
                       prod merge_acceptance_rate (matched deploy window)
```

When the two diverge, the unit tier is the problem (it's measuring
something that doesn't matter in prod) and needs new scenarios
from `select-golden`. When they agree, unit-tier scores are an
honest leading indicator and we can trust them as a deploy gate.

This calibration loop is what TURNS the bench into a stable
improvement engine; without it the bench is just regression
plumbing.

### C — observable cost

Every prompt edit changes both quality AND cost. The bench
captures both via OTel — `~/.diffgraph/traces.db` records tokens
in / out / cached + duration per agent run. The `efficiency` axis
of the five-axis scoring (TODO §5e.16) is the per-finding /
per-tool cost view; without it a "better" agent that takes 3×
more tokens looks like a wash on quality alone but is a
deployment cost regression. Currently shown on
`/qa/sessions/<run_id>` per-run; not yet aggregated as a separate
score axis.

## Daily / weekly / per-release rhythm

**Per commit (CI).**
- `pytest` in both repos — leak guards, unit-test logic checks
- TODO: smoke-fire 5-6 sentinel unit scenarios (`tier:smoke`,
  §5e.16) and fail loud if any score drops >10% from 7-day
  baseline. Today: not automated, run via /qa/plans on demand.

**Daily.**
- Integration tier nightly — full bench against real Bitbucket
- `pa fetch` to keep production data current
- Auto-fire any new scenarios added in the last 24h for an n=5
  baseline

**Weekly.**
- `pa trend --metrics merge_acceptance_rate,feedback_acceptance_rate
   --period biweek` → eyeball
- `pa select-golden` over the last 7 days → triage GOLD candidates,
  add 1-3 new scenarios
- `pa analyze-feedback` to keep the feedback judge cache warm

**Per release.**
- Full bench (unit + integration) on the release candidate commit
- Compare `/qa/scoring` against the previous release's baseline
  (the trend chart uses equal-spaced ordinal mutations so
  regressions show as level shifts between adjacent ticks, not
  smeared by attempt-count density)
- If `feedback_acceptance_rate` for the previous release dropped
  but the bench didn't catch it — that's a scenario coverage gap.
  Fire `select-golden` over the deploy window, mine the
  rejection-heavy PRs.

## Trace data model — uniform across agent modes

Every agent in the system — reviewer, investigator, dispatcher,
judge, lead — runs through the same five-event lifecycle in the
trace `events` table. The trace UI (`/qa/sessions/<run_id>`) and
the diagram builder (`tracing/server/diagram.py`) are
kind-agnostic: they don't branch on agent name or `runs.kind` or
the LLM provider. They just walk the events.

### Two step shapes

A step in any agent's run is one of two shapes:

```
   TOOL step                           TEXT step
   ─────────                           ─────────
   agent → system:<X>: call(args)      agent → system:human: <text>
   system:<X> → agent: result          (no paired result)

   The LLM dispatched one or more       The LLM returned only text —
   tools at this step (`done`,          no tool_calls. This is how
   `diff_read_file`, `pr_post_comment`,    mode:single agents (judges,
   `agent_spawn`, …). Each call has     lead agents that don't loop)
   a matching result on the next        deliver their output. Text
   step's request (or — for control-    "to the human" is treated as
   flow tools like `done` /             a tool call to a virtual
   `reflect` / `agent_spawn` — a        `system:human` target —
   self-arrow or parent-bound arrow).   same renderer path, no
                                        kind-special case.
```

The diagram speaks four event kinds — `tool_call`, `tool_result`,
`agent_spawn`, `agent_done`. **That's it.** Previously there were
also `agent_text` and `judge_verdict` kinds; both got folded into
`tool_call` to `system:human` so single-shot agents render with
the same code path as anything else.

### Five lifecycle events in `events` table

```
   agent_started          one per run, no step
   ┌── agent_llm_request   step N — what we sent to the LLM
   │   agent_llm_response  step N — what the LLM returned (content
   │                                + tool_calls, possibly empty)
   │   agent_step          step N — high-level marker (tools=...,
   │                                text_preview, token usage)
   │   agent_tool_request  step N — per-tool dispatch (one per
   │                                tool_call in the response)
   │   agent_tool_response step N — per-tool result
   │   agent_tool_result   step N — terminal tool-side event
   ├── ... (next step repeats the same block)
   agent_done             one per run, no step
```

`_run_react` (loop) and `_run_single` (one-shot) share one
helper — `Agent._observe_llm_call(step, messages, tools, do_call,
mode)` — which owns the `llm.request` OTel span, payload
stashing, and usage stamping. Both modes emit the same
AGENT_LLM_REQUEST / AGENT_LLM_RESPONSE / AGENT_STEP / AGENT_DONE
events. Single-shot just doesn't loop after step 0.

This is why **a judge's run looks like a one-step agent in
`/qa/sessions`**: it goes through `_run_single` → emits the
canonical five events → the diagram walks them like any other
agent. No fixture-side adapter, no kind detection in the
renderer.

### Step API contract (UI consumers)

The three `/api/runs/{run_id}/step/{agent_id}/{step}/*` endpoints
return a uniform shape regardless of step kind:

| Endpoint | Returns | Always-present for |
|---|---|---|
| `/messages` | full `messages` array — system + user + prior tool history. Same source for "request history" tab. | every step that emitted AGENT_LLM_REQUEST |
| `/call` | `{content, tool_calls}` envelope — the LLM's full assistant message. UI renders whichever is non-empty (tool args list for tool steps, text body for text-only steps). | every step that emitted AGENT_LLM_RESPONSE |
| `/result` | delta tool messages from the NEXT step's request — what the tools returned. 404 + "(no tool result for this step)" is the legit case for text-only steps and final-step `done`. | every step that had a successor step |

The session UI's `openStepDetails(agentId, step)` opens three
parallel-loaded tabs against these endpoints. History is the
step's OWN messages — what the agent saw when deciding step N —
so it works for the LAST step too (final `done` / final text
step). Previously it fetched step N+1's messages, which 404'd
for the last step.

## Repository topology

> **May-2026 merge:** the bench (`code-review-benchmarks/benchmark/`)
> is now a subtree of this repo at `benchmarks/` — history preserved
> via `git subtree`. One checkout, one `.venv`, one `.env`;
> `quality_api.config.bench_repo()` just returns the repo root. The
> ASCII diagram + table below still show the old two-repo split for
> historical context — read "code-review-benchmarks" as "the
> `benchmarks/` subtree" everywhere.

The QA system spans **three sibling repositories** under
`/home/andrey/repos/` (was four before the bench merge), each owning
a distinct concern. They communicate via shared SQLite databases and
a small contract:
agent comments carry a footer `` `dg:<generation>:<hash>:<run_id>` ``
that lets pr-analytics join its prod observations back to
diff-graph's trace storage by run_id.

```
┌──────────────────────────────────────────────────────────────────┐
│  /home/andrey/repos/                                             │
│                                                                  │
│  ┌──────────────────┐         ┌──────────────────────────────┐   │
│  │   diff-graph     │         │  code-review-benchmarks      │   │
│  │   (THIS REPO)    │←──cli──→│  (BENCH)                     │   │
│  │                  │subprocess│                              │   │
│  │  diffgraph/      │         │  benchmark/                  │   │
│  │  ├ bitbucket.py  │         │  ├ runner/                   │   │
│  │  ├ bitbucket_    │         │  │  ├ run.py     (integ)     │   │
│  │  │   fake.py ←───┼──env vars┼─├ run_unit.py  (unit)      │   │
│  │  ├ orchestrator  │         │  │  ├ fake_view.py           │   │
│  │  └ prompts/      │         │  │  ├ judge.py               │   │
│  │  orchestra/      │         │  │  └ scenario_loader.py     │   │
│  │  ├ agent.py      │         │  ├ scenarios/                │   │
│  │  └ trace_db.py   │         │  │  ├ unit/ → run-unit        │   │
│  │  quality_cli/    │         │  │  └ agents/ → run          │   │
│  │  └ main.py       │         │  ├ fixtures/                 │   │
│  │  quality_api/    │         │  │  ├ user-messages/         │   │
│  │  ├ queue.py      │         │  │  └ mocks/                 │   │
│  │  └ pools.py      │         │  └ tests/                    │   │
│  │  tracing/        │         └───────────┬──────────────────┘   │
│  │  └ server/app.py │                     │                      │
│  │    /api/qa/...   │                     ▼                      │
│  │    /qa/...       │         ┌──────────────────────────────┐   │
│  └────────┬─────────┘         │  code-review-examples        │   │
│           │ writes            │  (FIXTURE REPO)              │   │
│           ▼                   │                              │   │
│  ┌──────────────────┐         │  orderflow/    Java fixture  │   │
│  │ ~/.diffgraph/    │         │   (Spring Boot, multiple     │   │
│  │ ├ traces.db      │         │    feature/* + hotfix/*      │   │
│  │ │  runs/events/  │         │    branches, AGENTS.md,      │   │
│  │ │  qa_tasks/     │         │    intentional bugs)         │   │
│  │ │  qa_plans/...  │         │  scenarios/                  │   │
│  │ └ bench-runs/    │         └──────────────────────────────┘   │
│  │   <stamp>/.../   │                                            │
│  │     runs/        │                                            │
│  │       agent/     │         ┌──────────────────────────────┐   │
│  │       judge/     │         │  pr-analytics                │   │
│  └──────────────────┘         │  (PRODUCTION METRICS)        │   │
│           ▲                   │                              │   │
│           │ joins by run_id   │  pa/                         │   │
│           │ via dg-tag        │  ├ api.py        bitbucket   │   │
│           │ footer            │  ├ cmd_fetch.py  caching     │   │
│           │                   │  ├ cmd_merge_analysis.py     │   │
│  ┌────────┴─────────┐ ◄───────┤  ├ cmd_select_golden.py      │   │
│  │ bitbucket_       │  reads  │  ├ cmd_plot.py   trends      │   │
│  │ cache.db         │  cached │  └ dg_tag.py     parser      │   │
│  │ (~/.pr_analytics)│  prod   │                              │   │
│  └──────────────────┘  data   └──────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### Per-repo responsibilities

| Repo | Owns | Doesn't own |
|---|---|---|
| **diff-graph** | the agent (orchestra, prompts, tools, cli.py), trace storage (traces.db schema + writers), QA orchestration server (quality_api, tracing/server FastAPI), QA orchestration CLI (quality_cli), the FAKE bitbucket impl, **and `benchmarks/`** — scenario yamls (integration + unit), the judge (LLMJudge + judge.txt prompt), runners (run / run-unit), the fake-PR-view shim, leak-detection tests | the fixture repo content, prod metrics |
| **code-review-examples** | the orderflow fixture repo — Spring Boot Java app with intentional bugs, AGENTS.md, branches per scenario | anything CI-related; it's a static fixture |
| **pr-analytics** | production data cache (bitbucket_cache.db), merge_acceptance_rate / feedback_acceptance_rate LLM judges, select-golden pipeline, trend plotting | the agent, scenarios, bench |

### Cross-repo contracts

1. **diff-graph ↔ bench (subprocess + env vars).** Bench's
   `run_unit_fixture` shells out to diff-graph's `cli.py run`.
   Communication via:
   - Command flags: `--agent`, `--pr-url`, `--message`,
     `--mocks`, `--user-message-from`, `--invocations-out`,
     `--repo`, `--base`, `--source`, `--provider`
   - Env vars: `DIFFGRAPH_FAKE_PR_FILE` (payload path),
     `DIFFGRAPH_FAKE_PR_SINK` (JSONL sink path),
     `DIFFGRAPH_TRACE_PATH` (OTel trace dir),
     `DIFFGRAPH_SCENARIO_ID`, `DIFFGRAPH_SCENARIO_TAGS`,
     `BENCHMARK_TRACE_DIR` (set by quality_cli worker)
   - Shared SQLite: agent + judge both write to
     `~/.diffgraph/traces.db` `runs` rows linked via
     `linked_run_id`

2. **bench → code-review-examples (filesystem).** Each unit
   fixture has `repo.source: /home/andrey/repos/code-review-examples/orderflow`.
   `run_unit_fixture` `git clone --local --no-hardlinks` into a
   tempdir, checks out `source_branch`, and uses that as the
   PR-under-review. No pushes — the fixture repo is read-only.

3. **diff-graph (prod agent) → bitbucket (real PRs) → pr-analytics.**
   In production the agent posts comments with a footer:
   `` `dg:<generation>:<prompt_hash>:<run_id>` ``.
   pr-analytics' cache stage runs `extract_dg_tag()` per fetched
   comment and persists `(generation, hash, run_id)` columns on
   the comment row. This is the **join key**: a prod
   merge_acceptance verdict can be looked up against the agent's
   trace in `~/.diffgraph/traces.db` by run_id, so we know
   exactly which prompt version produced the rejected/accepted
   comment.

4. **bench → pr-analytics (one-way).** No direct wiring; bench
   doesn't read pr-analytics data programmatically. The human
   workflow is: `pa select-golden` writes `output/golden.html` →
   human reads → authors a new SCEN-NNN.yaml. Future automation
   could close this loop (TODO).

## Anonymous schedule — fire scenarios without saving a plan

A "saved" schedule (`qa_auto_plan_configs` table, configured via
`/qa/auto-plan` or yaml in `schedules/`) reruns automatically on
each new mutation in its lineage. An **anonymous** plan is a
one-shot equivalent: pick N scenarios + a specific (lineage, sha)
+ a provider, fire once, no rerun, no saved config. Lives in
`/qa/plans` like any other plan.

Use it when:
- a regression surfaced on /qa/scoring and you want to re-test
  one scenario on the current head before tweaking the prompt
- a new fixture just landed and you want an n=5 baseline before
  adding it to a saved schedule
- a prod merge_acceptance dip needs to be reproduced on a specific
  agent commit
- a /qa/sessions trace looks weird and you want a fresh reproduction

### CLI

```bash
quality-cli plans fire-anonymous \
    --scenario REV-U-001-store-credit-concerns \
    --scenario REV-U-002-cancel-npe-concerns \
    --sha 0c35ed4b2e6c \
    --lineage master \
    --provider deepseek
```

Repeat `--scenario` (or `-s`) for each fixture. Output:

```
plan #142 · anon:REV-U-001-store-credit-concerns:master@0c35ed4
  scenarios: REV-U-001-store-credit-concerns, REV-U-002-cancel-npe-concerns
  tasks created: 2
```

Then watch progress with:

```bash
quality-cli plans watch 142             # tail until all tasks finish
quality-cli plans get 142 --json         # one-shot status
quality-cli plans fire-anonymous ... --open-in-ui   # auto-open /qa/plans
```

The CLI is a thin wrapper around `POST /api/qa/fire-anonymous`. To
script outside the CLI:

```bash
curl -sX POST http://localhost:8765/api/qa/fire-anonymous \
  -H 'content-type: application/json' \
  -d '{
    "scenarios": ["REV-U-001-store-credit-concerns"],
    "sha":      "0c35ed4b2e6c",
    "lineage":  "master",
    "provider": "deepseek",
    "attempts_min": 5
  }'
```

Per-scenario `bench_cmd` is auto-detected from the fixture yaml —
unit-tier scenarios (yaml has `repo:`) route through `bench
run-unit`; integration scenarios fall through to the worker pool's
default cmd.

## Quality-CLI command reference

`quality-cli` (`quality_cli/main.py`) is a Typer app that hits the
trace-server's `/api/qa/*` endpoints. Configure `QUALITY_API_URL`
(defaults to `http://localhost:8765`). All commands support
`--json` for machine-readable output.

### `runs` — agent run history (the trace DB's `runs` table)

```bash
quality-cli runs list [--limit N] [--scenario X] [--agent Y]
quality-cli runs get <run_id>           # full trace metadata
```

### `tools` — what the agent called inside a run

```bash
quality-cli tools list --run <run_id>   # per-step tool name + args + result
```

### `agg` — pre-aggregated metric views

```bash
quality-cli agg by-provider [--scenario X] [--since DATE]
quality-cli agg by-scenario [--lineage L] [--mutation M]
```

### `traces` — drill into trace internals (the OTel side)

```bash
quality-cli traces ls [--run <run_id>]       # span tree
quality-cli traces session <session_id>      # full session
quality-cli traces events <run_id>           # raw events.jsonl
quality-cli traces event <event_id>          # one event
quality-cli traces otel <run_id>             # OTel spans
quality-cli traces problems [--since DATE]   # errors / orphans / warnings
```

### `plans` — orchestrate scenario fan-out

```bash
# Saved plan (creates qa_plans row + N qa_tasks rows)
quality-cli plans create \
    --scenario SCEN-009 --scenario REV-U-001-... \
    --lineage master --provider deepseek --attempts-min 5 \
    --name "release-2026-05-12-precheck"

# One-shot anonymous plan
quality-cli plans fire-anonymous \
    --scenario X --scenario Y \
    --sha <hash> --lineage L --provider P [--open-in-ui]

# Inspect / control
quality-cli plans list [--lineage L] [--status running|done|cancelled]
quality-cli plans get <plan_id>
quality-cli plans watch <plan_id>          # tail until done
quality-cli plans cancel <plan_id>
```

### `tasks` — individual scenario × attempt × provider work items

```bash
quality-cli tasks list [--plan P] [--state queued|running|...]
quality-cli tasks get <task_id>
quality-cli tasks enqueue --queue Q --bench-cmd "..."  # raw enqueue
quality-cli tasks retry <task_id>           # re-queue a failed task
quality-cli tasks cancel <task_id>
quality-cli tasks resolve <task_id>         # mark finished manually
quality-cli tasks reap [--max-age-hours N]  # close stale orphans
```

### `queue` — queue health

```bash
quality-cli queue stats                     # per-queue depth + lease state
```

### `auto` — saved schedules (qa_auto_plan_configs table)

```bash
# Configure
quality-cli auto add --name X --scenario SCEN-... --lineage L --provider P \
    [--trigger=on_commit|on_demand] [--attempts-min N]
quality-cli auto edit <config_id> --field=value
quality-cli auto delete <config_id>
quality-cli auto enable <config_id>
quality-cli auto disable <config_id>

# Manual fire of a saved schedule
quality-cli auto fire-on <config_id> --sha <hash> [--lineage L] [--open-in-ui]

# Discovery + browsing
quality-cli auto discover                    # surface new scenarios in bench
quality-cli auto branches                    # show known lineage branches
quality-cli auto list                        # configured schedules
quality-cli auto watch <config_id>           # tail recent fires

# Yaml import (when schedules/*.yaml is edited)
quality-cli auto reload-yaml
```

### `worker` — supervisor + pools

```bash
quality-cli worker --queue Q --bench-cmd "..." \
    --max-idle-seconds 600 --task-timeout-seconds 1800 --max-tasks 0
```

Usually run by the supervisor (`quality_api/pools.py`) — but the
flag set is available for manual single-worker debugging.

## Server API reference

Mounted on the trace server (`tracing/server/app.py`,
default `http://localhost:8765`). Three families: `/api/runs/*`
(read trace data), `/api/qa/*` (orchestration), `/qa/*` (HTML
pages).

### Traces (read-only)

| Endpoint | Purpose |
|---|---|
| `GET /api/runs` | list recent runs (filterable) |
| `GET /api/runs/{run_id}` | one run's metadata |
| `GET /api/runs/{run_id}/json` | full trace as JSON |
| `GET /api/runs/{run_id}/events` | events.jsonl stream |
| `GET /api/runs/{run_id}/step/{agent_id}/{step}/{messages,call,result}` | per-step details for `/qa/sessions` drill-in |
| `GET /api/otel/traces/{run_id}` | OTel-format spans |
| `GET /api/diagram?scope=<uri>&format=<f>` | scenario diagram in mermaid / d2 / g6 / events / svg |

### Search + aggregation

| Endpoint | Purpose |
|---|---|
| `GET /api/search/runs` | search runs by scenario / lineage / mutation / provider |
| `GET /api/search/sub_runs` | sub-agent rows (spawned investigators etc.) |
| `GET /api/search/tool_calls` | tool invocations across runs |
| `GET /api/search/dimensions` | distinct values per filterable column — drives /qa/* dropdowns |
| `GET /api/search/aggregates/by_provider` | provider-level rollups |
| `GET /api/search/aggregates/by_scenario` | scenario-level rollups |
| `GET /api/search/aggregates/by_mutation` | mutation-level rollups |
| `GET /api/search/scoring/{mutation}` | per-mutation per-scenario score grid |
| `GET /api/search/scoring-compare` | A/B compare two mutations |
| `GET /api/search/per_run_scores` | flat per-run scores (drives /qa/scoring's trend + boxplot) |
| `GET /api/search/compare` | generic compare endpoint |

### QA tasks (the work queue)

| Endpoint | Purpose |
|---|---|
| `POST /api/qa/tasks` | create one task (rarely used directly — usually via plans) |
| `GET /api/qa/tasks` | list tasks (by plan / state / scenario) |
| `GET /api/qa/tasks/{task_id}` | one task |
| `POST /api/qa/tasks/lease` | worker calls — lease the next ready task |
| `POST /api/qa/tasks/{task_id}/heartbeat` | worker keeps lease alive |
| `POST /api/qa/tasks/{task_id}/finish` | worker marks task done |
| `POST /api/qa/tasks/{task_id}/cancel` | cancel a queued / running task |
| `POST /api/qa/tasks/{task_id}/retry` | re-queue a failed task |
| `POST /api/qa/tasks/enqueue` | bulk-enqueue from a saved schedule's spec |
| `POST /api/qa/tasks/reap` | close stale orphans (no heartbeat for N seconds) |
| `GET /api/qa/resources/resolve` | resolve `scenario://X` / `lineage://Y` / etc. URIs |
| `GET /api/qa/resources/kinds` | list known URI schemes |

### QA plans (a plan = a group of related tasks)

| Endpoint | Purpose |
|---|---|
| `POST /api/qa/plans` | create a saved plan (cross-product of lineages × providers × scenarios × attempts) |
| `GET /api/qa/plans` | list plans |
| `GET /api/qa/plans/{plan_id}` | one plan + progress |
| `POST /api/qa/plans/{plan_id}/cancel` | cancel all queued tasks in this plan |
| `POST /api/qa/fire-anonymous` | one-shot anonymous plan (see "Anonymous schedule" above) |

### QA scenarios catalog (bench's scenario tree)

| Endpoint | Purpose |
|---|---|
| `GET /api/qa/scenarios` | recursive list of bench's scenarios/* — drives `/qa/scenarios` multi-select |
| `GET /api/qa/scenarios-catalog` | flat catalog with bench_cmd templates |

### Auto-plan schedules (saved configurations that auto-fire)

| Endpoint | Purpose |
|---|---|
| `GET /api/qa/auto-plan/configs` | list saved schedules |
| `GET /api/qa/auto-plan/configs/{config_id}` | one schedule |
| `POST /api/qa/auto-plan/configs` | create |
| `PUT /api/qa/auto-plan/configs/{config_id}` | update |
| `DELETE /api/qa/auto-plan/configs/{config_id}` | remove |
| `POST /api/qa/auto-plan/configs/{config_id}/fire-on` | manually fire a saved schedule on a specific (lineage, sha) |
| `GET /api/qa/auto-plan/configs/{config_id}/history` | past fires + their plan_ids |
| `GET /api/qa/auto-plan/configs/{config_id}/preview-scenarios` | dry-run scenario resolution |
| `POST /api/qa/auto-plan/discover` | surface new scenarios in bench |
| `POST /api/qa/auto-plan/reload-yaml` | re-import `schedules/*.yaml` (yaml wins over DB-edited values) |

### Workers + worker-pools (the bench subprocess supervisor)

| Endpoint | Purpose |
|---|---|
| `POST /api/qa/workers` | worker registers itself on startup |
| `POST /api/qa/workers/{worker_id}/heartbeat` | keep registration alive |
| `GET /api/qa/workers` | list active workers |
| `POST /api/qa/workers/cleanup-dead` | drop workers that missed heartbeats |
| `POST /api/qa/worker-pools` | create a pool (queue + bench_cmd template + max-idle) |
| `GET /api/qa/worker-pools` | list pools |
| `PUT /api/qa/worker-pools/{pool_id}` | update a pool |
| `DELETE /api/qa/worker-pools/{pool_id}` | remove a pool |
| `GET /api/qa/queues` | per-queue stats (depth, lease ratio, alive worker count) |
| `GET /api/qa/config` | runtime config (default bench_cmd, server_url, etc.) |

### HTML pages

| Page | Purpose |
|---|---|
| `GET /qa/` | landing |
| `GET /qa/sessions` | session list (was /qa/traces; legacy URLs 308) |
| `GET /qa/sessions/{run_id}` | one session drill-in (diagram + tree/table) |
| `GET /qa/plans` | plans + their tasks |
| `GET /qa/scenarios` | scenario catalog + fire-on selector |
| `GET /qa/queue` | queue browser |
| `GET /qa/auto-plan` | saved schedule editor |
| `GET /qa/workers` | worker + pool status |
| `GET /qa/scoring` | score charts (trend / distribution / per-attempt) |
| `GET /qa/mutations` | per-mutation rollup |

## Reference

- `benchmarks/README.md` — bench user guide
- `benchmarks/AGENTS.md` — bench architecture (loaders, factories, judge interface)
- `benchmarks/runner/run_unit.py` — unit tier runner + judge wiring
- `benchmarks/runner/fake_view.py` — fake-bitbucket view for the judge
- `diff-graph/diffgraph/bitbucket_fake.py` — class-based fake provider
- `diff-graph/tracing/README.md` — trace storage + CLI
- `diff-graph/TODO.md` §5d.3, §5e.14, §5e.16 — open work items in the QA roadmap
- `pr-analytics/README.md` — production metrics + DSL
- `pr-analytics/pa/cmd_merge_analysis.py` — merge_acceptance_rate implementation
- `pr-analytics/pa/cmd_select_golden.py` — GOLD/SILVER PR selector

## Glossary

- **Tier:unit** — one agent in isolation, fake bitbucket, fast judge. Predicts regressions.
- **Tier:integration** — full pipeline, real bitbucket. Catches end-to-end issues.
- **Tier:smoke** — TODO: a 5-6 sentinel subset of unit tier fired on every commit.
- **Tier:chaos** — TODO: resilience scenarios (rate limit, OOM, malformed input). Resilience axis.
- **GOLD PR** — production PR with high deep-comment density and high acceptance, classified as bench-worthy by `pa select-golden judge` phase.
- **Leak** — fixture input contains the keyword its expected_output grades on. Caught by `test_unit_fixture_leak_check.py` / `test_prompts_no_fixture_leak.py`.
- **assert_via** — which channel the judge reads to score: `pr_comments` (real comments via fake-PR sink), `intended_findings` (done(findings) args), `intended_concerns` (reflect(questions_remaining) args).
- **linked_run_id** — column on `runs` rows linking an agent run to its judge counterpart. Set by `LLMJudge._finish_trace()`. Required for `/qa/scoring` to surface a scored row.
