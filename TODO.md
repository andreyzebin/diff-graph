# Orchestra — Planned Improvements

## Done (implemented)

- ~~3.1 Concerns instead of questions~~ — lead uses 3-5 concerns, reviewer breaks into sub-questions
- ~~3.2 Question cap (max 5)~~ — in lead prompt
- ~~3.3 Question IDs in SGR schema~~ — IDs with PUT semantics, fuzzy matching fallback
- ~~3.4 Fuzzy matching in SGR~~ — >50% word overlap matches existing question
- ~~1.5 Child cost in spawn results~~ — spawn returns steps, tokens, sgr_summary
- ~~Budget cache discount~~ — cumulative_paid = sum of per-step deltas, cache_discount=0.1
- ~~Child agents use own .prompt budget~~ — not parent-allocated
- ~~Default pushers from compiler~~ — 75% nudge + 100% force_done for all agents
- ~~SQLite trace DB~~ — events persisted per-step, crash-safe
- ~~HTML trace~~ — split-pane, [⧉] detail tabs, [📋 JSON] copy, Call→Result→Context
- ~~Trace web server~~ — FastAPI + Alpine.js, navigator + live views, separate routes, bulk load + WebSocket, color-coded child agents, auto-scroll, runs list polling, ↑↓© token display, tool args preview, Copy/JSON toolbar
- ~~CLI trace command~~ — --log, --list, --run, browser auto-open
- ~~Data inheritance~~ — parent data_scope auto-injected into child {placeholders}
- ~~No handoff context by default~~ — child gets everything via system prompt
- ~~Concerns scale to diff size~~ — 1-2 small, 2-3 medium, 3-5 large. No splitting one issue.
- ~~Reviewer: investigate first, reflect after~~ — get_diff/read_outline before first reflect
- ~~SGR reflect rules~~ — don't open already-known questions, don't reflect twice in a row
- ~~Lead doesn't hand off SGR~~ — prompt instructs not to pass context_handoff
- ~~Thread-safe trace DB~~ — threading.Lock on SQLite writes for parallel agents
- ~~Orphan agents in trace~~ — unlinked agents attached to root, all visible
- ~~strategist → lead rename~~ — everywhere in code, prompts, docs
- ~~DiffSearch VFS~~ — virtual unified diff filesystem with ref= param, lazy materialization, 105 tests
- ~~Webhook router~~ — Bitbucket webhook with A/B routing, forward/command modes, sample cascade, 31 tests
- ~~Resource providers~~ — file:// and bitbucket:// for prompt loading, --prompts CLI flag
- ~~Prompt generations in runs UI~~ — prompt_source + prompt_hash (commit SHA or content md5) in trace DB, visible in runs list

---

## 1. Budget Awareness & Cost Control

### 1.1 Budget context injection at start

Inject budget context before lead's first LLM call:

```
BUDGET CONTEXT:
  Total: 50,000 tokens / 50 steps
  Diff size: 26,000 chars / 9 files
  Reviewer budget: 15,000 tokens / 20 steps (from reviewer.prompt)
  Max affordable reviewers: ~3 (with reserve for consolidation)
```

**Where:** `diffgraph/orchestrator.py` — inject as user message.
**Effort:** Small.

### 1.2 Smart pushers (runtime-computed messages)

Replace static pusher messages with callbacks that include current budget state:

```
"75% budget used (11,250 of 15,000 paid). Wrap up current investigation."
```

**Where:** `orchestra/budget.py` — pusher callback receives BudgetState.
**Effort:** Medium.

### 1.3 Pre-spawn budget validation

When `spawn_many` is called, estimate total child cost. If too expensive, reduce N:

```
spawn_many(4 reviewers): need ~60k, have 40k → spawn 2, merge tasks
```

**Where:** `orchestra/agent.py` `_meta_spawn_many`.
**Effort:** Medium.

### 1.4 `budget_status` meta-tool

Tool the lead can call to see remaining budget, children cost, affordable count.

**Where:** `orchestra/tools/builtin.py` + `orchestra/agent.py`.
**Effort:** Small.

### 1.6 Wall-clock pusher with hierarchy propagation

**Context.** Java reviewer scenarios timeout at the 600s bench CLI cap and get
SIGKILL'd before posting findings (SCEN-009/010 in the May-2026 matrix:
4 investigators × 10–12 steps each, never `done`'d). The wall-pusher
infrastructure already exists but is unused, and parent pushers don't
reach live children — investigators keep burning wall time while the
reviewer's pusher has already fired.

**What's already in place** (do not rebuild):

- `BudgetState.wall_ratio()` (`orchestra/budget.py:70`) — `elapsed / max_wall_time`
- `BudgetState.max_ratio()` includes wall in the value `check_pushers()` compares against `pusher.at`
- `_parse_budget_header` (`orchestra/compiler.py:443`) understands `s`/`m` suffixes in `@budget` — but nothing currently uses them
- `Agent._children: dict[str, Agent]` (`orchestra/agent.py:163`) holds every live descendant
- `Agent.inject_message(msg)` is thread-safe (`agent.py:253`) and drains at the start of each step (`agent.py:310`)

So both halves of the mechanism are wired — they just don't talk to each other.

**Design.**

1. `PusherConfig.propagate: bool = False` — when true, the pusher's action also fires on every live child.
2. `_apply_pusher` (`agent.py:1101`): after the local effect, if `action.propagate`, iterate `self._children.values()` and call `child.inject_message(action.message)`. Don't restrict child tools — let the child decide whether the message means "scope down" or "finalize" based on its own ratio.
3. Default pusher set (in `_parse_budget_header`):
   - `0.50: NUDGE propagate` — "halftime, pick most impactful direction"
   - `0.75: NUDGE propagate` — "75%, wrap current investigation, prepare findings"
   - `1.00: FORCE_DONE propagate` — restrict tools to `done`, message inherits
4. Per-agent wall budgets in prompt headers:
   - `reviewer.prompt`: `@budget: 50000 tokens, 50 steps, 900s`
   - `investigator.prompt`: `@budget: 15000 tokens, 20 steps, 240s`
   - `dispatcher.prompt`: `@budget: 30000 tokens, 10 steps, 60s`
5. Bench harness: `triggers.cli.timeout: 1200` in `code-review-benchmarks/benchmark/config.local.yaml` — leaves headroom over the reviewer's 900s.

**Why propagate by default.** A parent's "finalize" push that doesn't reach live leaves is empty calories: the parent is blocked on `join`, time keeps advancing, leaves keep exploring. The natural behavior we want is leaves wrap up first → parent unblocks → synthesizes → calls own `done`. Propagation makes that automatic.

**Edge cases / decisions to make:**

- Should NUDGE propagation include the parent's ratio in the child's message ("parent at 75% wall, you have ≈Y seconds left")? Probably yes — children make better decisions with the absolute deadline, not just a vague "wrap up".
- Should `FORCE_DONE` propagate restrict child tools too? Probably yes for symmetry — child should also collapse to `done`.
- `allocate_child` already gives a child a fraction of the parent's *remaining* wall — passive propagation for new spawns. Active propagation (this item) covers already-running spawns.

**Where:** `orchestra/types.py` (PusherConfig), `orchestra/agent.py` (_apply_pusher), `orchestra/compiler.py` (default pushers), three `.prompt` headers, bench `config.local.yaml`.

**Effort:** Small. The hardest part is naming and writing the messages well.

---

### 1.5 Historical cost tracking with complexity tiers

Track cost across runs indexed by complexity (tiny/small/medium/large). Percentile distributions per model. Feeds into budget context injection.

Already have SQLite trace DB — can compute from events table:
```sql
SELECT percentile(paid, 0.75) FROM events
WHERE event_type='agent_llm_response' AND run_id IN (
  SELECT id FROM runs WHERE model='deepseek-chat'
)
```

**Where:** `orchestra/trace_db.py` — add query methods. Or new `orchestra/cost_tracker.py`.
**Effort:** Medium.

---

## 2. Parallel Agent Observability

### 2.1 Agent prefix in trace --log

When showing parallel children in `trace --log`, prefix with agent short id:

```
[R1] step 0  get_diff
[R2] step 0  get_diff
[R1] step 1  read_outline(PricingService.java)
```

Currently child events are suppressed in live CLI (only root shown). But `trace --log` shows all agents — needs prefix.

**Where:** `cli.py` `_print_trace_log`.
**Effort:** Small.

### 2.2 Live progress for parallel children

Show brief status while spawn_many is running:

```
  lead  spawning 4 reviewers…  [R1: step 3] [R2: step 5] [R3: step 2] [R4: done]
```

**Where:** `cli.py` — subscribe to child events during spawn_many.
**Effort:** Medium.

---

## 3. Prompt Quality

### 3.1 Budget balance instruction in lead prompt

```
BUDGET MANAGEMENT:
  Your total budget is shared with all reviewers you spawn.
  Each reviewer costs ~5,000-10,000 tokens.
  Better to spawn 2 thorough reviewers than 4 starved ones.
```

**Where:** `diffgraph/prompts/lead.prompt`.
**Effort:** Small.

### 3.2 Reviewer efficiency prompt

```
Work efficiently:
- Use read_outline before read_file to target specific lines
- Don't re-read files you've already seen
- If budget running low, focus on highest-priority finding
```

**Where:** `diffgraph/prompts/reviewer.prompt`.
**Effort:** Small.

### 3.3 Diff filtering in system prompt

Don't include gradle wrapper, binary files, and other noise in diff_summary. Filter before injecting.

**Where:** `diffgraph/orchestrator.py` `_make_diff_summary`.
**Effort:** Small.

---

## 4. Trace Web Server

### 4.1 FastAPI + HTMX trace server

Replace static HTML with a web server for browsing, live viewing, and comparing traces.

**Architecture:**
```
orchestra/
  trace_server/
    __init__.py          # create_app()
    app.py               # FastAPI routes
    live.py              # WebSocket for real-time trace
    templates/
      base.html          # layout + nav
      runs.html          # run list (filter, search, sort)
      trace.html         # trace detail (split-pane with tabs)
      compare.html       # side-by-side diff
    static/
      trace.css          # extracted from current _CSS
      trace.js           # extracted from current _JS
```

**Routes:**
```
GET  /                    → run list (filterable, searchable)
GET  /runs/{id}           → trace detail (current split-pane UI)
GET  /runs/{id}/json      → API: raw trace data
GET  /compare?a=X&b=Y    → side-by-side comparison
WS   /ws/live/{run_id}   → live trace updates via WebSocket
```

**CLI integration:**
```bash
python cli.py serve                        # start on localhost:8080
python cli.py serve --port 9000            # custom port
python cli.py run --pr-url ... --serve     # run + open live trace in browser
```

**Key features:**
- **Live tracing** — WebSocket pushes new events during run. See lead analyzing, reviewers investigating, findings appearing in real-time.
- **Run history** — browse, filter by model/severity/date, search by finding title
- **Comparison** — diff two runs side-by-side (concerns, findings, cost)
- **Team sharing** — `serve --host 0.0.0.0` → colleagues open the URL
- **Code viewer** — show source files with findings highlighted inline
- **CI integration** — `POST /api/runs` for automated reviews

**Phase 1 (extract static files + basic server):** ✅ Done
**Phase 2 (live tracing via WebSocket):** ✅ Done
**Phase 2.5 (custom scrollbars + draggable divider):** ✅ Done

**Phase 3 (Alpine.js + HTMX):**
- Replace vanilla JS with Alpine.js (~15KB) for declarative client interactivity
- Add HTMX for server-rendered navigation (run list filtering, pagination)
- Tabs: `x-data` + `x-for` instead of manual DOM manipulation
- WebSocket live view: Alpine store + `x-for` rendering
- Copy/toast: `x-show` + `x-transition`
- Resizable divider: Alpine `x-on:mousedown`
- No build step — CDN or vendored scripts
- **Effort:** Medium.

**Phase 4 (comparison + search):**
- Side-by-side comparison view
- Search across runs by finding/file/severity
- HTMX-powered filtering on run list
- **Effort:** Medium.

**Dependencies:** `fastapi`, `uvicorn`, `jinja2`, Alpine.js (CDN), HTMX (CDN).

### 4.2 Trace export to JSON

```
python cli.py trace --run ID --format json > trace.json
```

**Where:** `cli.py` trace command + `orchestra/trace_db.py`.
**Effort:** Small.

### 4.3 Trace search (CLI)

```
python cli.py trace --search "auth" --severity MAJOR
```

**Where:** `orchestra/trace_db.py` — SQL queries.
**Effort:** Small.

---

## 5. CLI Improvements

### 5.1 Total cost summary at end

After findings, show cost breakdown:
```
Cost: 12,500 tokens paid (lead: 4,200 + reviewer×2: 4,150 each)
      22 steps total, 45s wall time
```

**Where:** `cli.py` — accumulate from events.
**Effort:** Small.

### 5.2 Model comparison mode

Run same PR with two models, compare results:
```
python cli.py run --pr-url ... --model gpt-4o --compare deepseek-chat
```

**Where:** `cli.py` — run twice, diff findings.
**Effort:** Medium.

---

## 5c. Pre-deploy QC pipeline — outer scheduler (across generations and mutations)

> Two schedulers in this codebase, intentionally separate:
> - **5c (this section)** — outer/QC-pipeline scheduler. Plans which
>   `(branch, sha) × provider × scenario` units to run, queues them
>   over time, owns the gentle/aggressive policy *across the whole
>   QC matrix* of generations + mutations.
> - **[5d](#5d-per-run-scheduler--gentle--aggressive-inside-one-bench-cli-run)** — inner/per-run scheduler. Lives inside one
>   `bench/cli.py run` invocation, regulates how its own
>   (provider × scenario × attempt) tasks fan out: gentle = one at
>   a time (current behaviour), aggressive = parallel via
>   temp-branch PRs (one throw-away branch per task so the
>   `[BENCHMARK]` PRs don't collide on (from_branch, to_branch)).

### Why this exists (background)

A prompt or framework change to the agent goes out to many real PRs.
A bad mutation lands as comment noise in production code reviews,
trains the team to ignore the bot, and costs us merge-acceptance —
the headline business metric. The bench can catch most of this *if*
we run it consistently and *if* we don't deploy until the result
holds across attempts.

This subsystem is the gate: nothing rolls out until the new branch
has cleared a fixed bar across several attempts on each of the
production-facing LLMs (qwen3-6, qwen3-coder). The harness has to be
gentle enough to coexist with the corp LLM endpoints' real traffic
(they're shared, not ours alone) and aggressive enough to catch up
when the mutation queue grows fast.

### One CLI, one config

Single CLI: `bench-schedule` (under `code-review-benchmarks/qa/`).
Subcommands: `discover`, `plan`, `run`, `report`. One process per
command — no daemons; cron / systemd-timer wakes them up.

Single config: `code-review-benchmarks/qa.local.yaml` (gitignored).
Holds providers, rate limits, sentinel scenario list, repo paths,
agent CLI invocation template (so the scheduler can pass through
`--prompts`, `--bot-user`, etc. without a second config file).
Example shape:

```yaml
qa:
  repo: "/home/andrey/repos/diff-graph"   # repo with the mutations
  github_remote: "origin"                 # where branches arrive
  branches_glob: ["feature/*", "mutation/*"]
  providers: ["qwen3-6", "qwen3-coder"]   # required for deploy-ready
  attempts_min: 3                         # ≥3 per (provider, scenario)
  mode: "gentle"                          # gentle | aggressive
  rate_limit:                             # gentle-mode token bucket
    qwen3-6: { requests_per_min: 30 }
    qwen3-coder: { requests_per_min: 12 }
  agent_cli:                              # passed through to bench
    prompts_uri_template: "file://{repo}@{commit}/diffgraph/prompts"
    bot_user: "tuz_spasibo__qodo"
    subject_pattern: '^\[(\w+)\]\s+'
    verdict_mode: "comment"
  sentinel: ["SCEN-009", "SCEN-011", "SCEN-203", "SCEN-301"]
  state_dir: "qa/state/"                  # qa-state.json lives here
```

The agent CLI block is the one channel the scheduler uses to invoke
diff-graph; everything that needs to flow into the bench (and from
there into diff-graph) goes here. No second config file.

### Discovery

`bench-schedule discover`:
- `git fetch <github_remote>` (no GitHub API; just git).
- For each ref matching `branches_glob`, compare its tip SHA to
  `qa-state.json["last_qc_commit"][branch]`.
- New SHA → emit `(branch, sha)` rows for the planner to pick up.
- New commits on a branch supersede the prior QC runs of that branch
  for deploy-readiness; the historical runs stay in the metrics
  table for trend analysis but don't gate the new commit.

### Plan

`bench-schedule plan`:
- Reads `discover` output (or accepts `--branches` explicitly).
- Cross-product against providers (config) × scenarios (filter).
- Emits a JSONL queue file in `state_dir/queue-<timestamp>.jsonl`.
- One queue row = one task: `{branch, mutation_hash, provider,
  scenario, attempt_n}`.

Scenario filters:
- `--scenarios all` — everything in `scenarios/` minus `cost:expensive`.
- `--scenarios sentinel` — the curated list from config (`config.sentinel`).
- `--scenarios hard` — the `cost:expensive` set, opt-in.
- `--scenarios SCEN-NNN,...` — explicit list.

Sentinel scenarios are the small subset that historically
discriminates prompt quality fastest. Calibration: run a candidate
sentinel set on the last N mutations, compute per-scenario score
variance across mutations; the top-K by variance go in
`config.sentinel`. Re-calibrate periodically (every ~50 mutations).
Starter sentinel until we have data: `SCEN-009, SCEN-011, SCEN-203,
SCEN-301` — the four with the most signal in current bench runs.

### Run (the scheduler)

`bench-schedule run --queue state/queue-<ts>.jsonl`:

Two modes (config flag, also `--mode` override):
- **gentle**: a single worker per provider; token bucket throttles
  to `requests_per_min`. Multiple providers run side-by-side (each
  a worker), but within a provider the rate is capped. Coexists
  nicely with shared corp LLM traffic.
- **aggressive**: as much parallelism as CPU + LLM concurrency
  allows. ThreadPoolExecutor with `--max-concurrency`. For weekend
  catch-up runs.

Per task the worker:
1. Checks out `branch@sha` into a per-task worktree (so concurrent
   workers don't collide on `repo`).
2. Resolves `agent_cli.prompts_uri_template` to a concrete URI
   pointing at the mutated prompts; passes through to the bench
   scenario runner.
3. Runs the scenario via the existing bench harness (`benchmark/cli.py`).
4. Records the metrics row (see below).
5. Cleans up the worktree.

Worker resilience:
- Network blip on LLM → existing `_llm_call_with_retry` helper.
- Bench timeout (CLI timeout exceeded) → metrics row with
  `verdict=error`, `error_class=timeout`. Doesn't poison the queue.
- SIGINT in the middle → the in-flight task is left as `started_at`
  set / `finished_at` null in the DB, picked up by `bench-schedule
  reaper` on the next discover cycle.

### Metric categories

Two tracked categories — **hard skill** and **collaboration**. Each
mutation gets a separate score per category. Both must clear their
threshold for deploy-readiness.

**Hard skill (review quality)** — the headline category. Drives
business merge-acceptance directly: a reviewer agent that catches
real defects with low false-positive rate is what gets its comments
acted on. Measured from review-style scenarios:

- `recall = required_found / required_total` — did we catch the
  expected defects?
- `precision = 1 - false_positives / total_findings` — how much
  noise did we add?
- `severity_calibration = fraction of found findings whose severity
  bucket matches expected.severity` — BLOCKER vs MAJOR vs MINOR
  vs COMMENT alignment.
- `verdict_match = NEEDS_WORK/APPROVED matches expected_status_change`.
- Aggregate: weighted F-score-like metric, weights in config.
- Source scenarios: SCEN-009, SCEN-010, SCEN-011, SCEN-300, SCEN-301,
  SCEN-303, SCEN-304 (all review / incremental scenarios).

**Collaboration (thread interaction)** — secondary but real: the
agent has to read PR threads coherently, answer questions inside the
discussion they live in, and not duplicate work others already did.
Measured from interaction scenarios:

- `thread_focus = must_mention rows that match` (existing reply judge
  signal) — did the agent answer in the right thread context?
- `must_address = explicit answer to the question` — did it actually
  answer, or hedge?
- `forbidden_off_topic_rate` — did it stay on the asked topic?
- `incremental_awareness` — for prior-round scenarios (SCEN-300,
  SCEN-301, SCEN-302 when it lands): did the agent acknowledge the
  prior-round comments rather than re-flag them?
- Source scenarios: SCEN-200, SCEN-201, SCEN-202, SCEN-203 +
  the prior-round half of SCEN-300/301.

The judge already produces `agent_warnings` (kinds:
wrong-location, wrong-reasoning, surface-acceptance,
contradicts-codebase, methodology-gap, interface-violation, other).
Both categories incorporate the count of these as a quality
penalty — prompts that get the right answer with bad reasoning
shouldn't outrank prompts that reason cleanly.

### Metrics storage

For now: a single namespace in pr-analytics (no separate prod-vs-QC
split). New table:

```
agent_qa_runs (
  id INTEGER PRIMARY KEY,
  branch         TEXT,        -- e.g. feature/parallel-bench
  mutation_hash  TEXT,        -- short SHA, indexes the prompt revision
  provider       TEXT,        -- qwen3-6 / qwen3-coder
  scenario       TEXT,        -- SCEN-NNN
  attempt_n      INTEGER,     -- 1..attempts_min
  -- hard skill axes (NULL when not applicable to this scenario type):
  hs_recall      REAL,
  hs_precision   REAL,
  hs_severity    REAL,
  hs_verdict_ok  INTEGER,     -- 0/1
  hs_score       REAL,        -- aggregate
  -- collaboration axes:
  co_thread_focus    REAL,
  co_must_address    REAL,
  co_off_topic_rate  REAL,
  co_score           REAL,
  -- shared:
  agent_warnings_count    INTEGER,
  scenario_warnings_count INTEGER,
  duration_seconds        REAL,
  trace_path              TEXT,
  verdict                 TEXT,    -- pass / fail / error
  error_class             TEXT,    -- timeout / api / null
  started_at              DATETIME,
  finished_at             DATETIME
)
```

Aggregate view (`agent_qa_mutation_summary`): per
(branch, mutation_hash) → median(hs_score), median(co_score),
attempts_count, deploy_ready boolean.

Decision rule for `deploy_ready = true`:
- `attempts_count >= attempts_min` for every (provider, scenario)
  in the configured set.
- `min over (provider, scenario) of median(hs_score) >= hs_threshold`.
- `min over (provider, scenario) of median(co_score) >= co_threshold`.
- No errored attempts in the latest batch.

### Report

`bench-schedule report --mutation <hash>`:
- Markdown table per provider × scenario.
- Per-category pass/fail with median + (min..max) range over attempts.
- Top-3 agent_warnings by frequency for the mutation.
- Trace dir paths inline.
- Exit code: 0 if deploy-ready, 1 otherwise — usable as a CI gate.

`bench-schedule report --branch <name>`: same shape but rolled up
across the branch's mutations.

### Dashboard (later, in pr-analytics)

Routes:
- `/qa/branches` — list of QC'd branches with state (running /
  ready / failed).
- `/qa/mutations` — per-hash, two-category scoreboard (hard skill
  vs collaboration), deploy-ready flag.
- `/qa/runs` — live + recent run rows.
- `/qa/runs/{id}` — drill: live log tail (websocket), trace link,
  judge response, agent_warnings list.
- `/qa/scheduler` — queue depth, active workers, rate-bucket state,
  current mode.

UI nice-to-haves:
- Per-mutation timeline showing which (provider × scenario) cells
  passed/failed, click-through to the run.
- Agent warnings heatmap across mutations — which kinds are
  trending.

### MVP order (one slice at a time)

1. **Config + discover + plan** — read `qa.local.yaml`, run git
   fetch, diff vs `qa-state.json`, write JSONL queue.
2. **Run in gentle mode, single worker, no metrics yet** — just
   prove the scheduler invokes the bench correctly and writes
   trace dirs.
3. **Metrics persistence** — new pr-analytics table + writer in the
   scheduler. Two-category scoring computed from existing judge
   outputs (judge already gives required_comments, false_positives,
   reply.must_mention/must_address, agent_warnings).
4. **Report CLI** — markdown table, deploy-ready exit code.
5. **Multi-worker gentle mode** — token bucket per provider.
6. **Sentinel calibration helper** — `bench-schedule calibrate
   --on N` runs the candidate sentinel on N mutations, picks
   highest-variance scenarios.
7. **Aggressive mode** — `--max-concurrency`, ThreadPoolExecutor.
8. **Dashboard** — pr-analytics web routes, websocket live logs.

Steps 1-4 = end-to-end QC gate. Steps 5-8 = scaling and ergonomics.

### Where it lives

- Planner + scheduler + report CLI: `code-review-benchmarks/qa/`,
  one entry point `bench-schedule` (typer).
- Tracking files (`qa-state.json`, queue files): `qa/state/`,
  gitignored.
- Worktree dir for concurrent workers: `qa/worktrees/<task-id>/`,
  gitignored, cleaned up by the worker.
- Metrics tables + dashboard routes: `pr-analytics/` (existing
  FastAPI app extended).

### Future (user-driven, not in scope)

Evolution orchestrator on top of QC:
- Watches QC outcomes per mutation.
- Spawns new mutations along the weakest category-axis (hard skill
  vs collaboration) using the worst-discriminating scenarios.
- Cross-breeds top-K dominant prompts (semantic merge — not text
  merge — via an LLM-as-merger).
- Gradually rolls out (sample %), tracks downstream merge
  acceptance & developer feedback as fitness signal.
- Forms hypotheses for the next round.

The QC pipeline is the substrate this loop sits on; user is
implementing the orchestrator personally.

---

## 5d. Per-run scheduler — gentle / aggressive inside one bench CLI run

> Inner scheduler, paired with [5c](#5c-pre-deploy-qc-pipeline--outer-scheduler-across-generations-and-mutations).
> 5c plans the QC matrix; 5d plans how one matrix cell (one
> `bench/cli.py run` invocation) farms its own work out.

**Why.** A single `bench run -p <provider> -s <scenarios> -n <repeat>`
is itself a small matrix: providers × scenarios × attempts. Today it
runs strictly sequentially, which is fine when the user wants to be
polite to a shared LLM endpoint, but slow for pre-merge smoke when
the goal is "burn through the pool as fast as possible before I hit
merge". Same gentle/aggressive lever as 5c, applied at the level of
one CLI invocation.

**Modes.**
- **gentle** (default) — sequential, one task at a time. Current
  behaviour. Polite to shared LLM endpoints; matches typical
  developer "iterate locally" usage. No new infra needed.
- **aggressive** — bounded-parallel via `asyncio.Semaphore`. Each
  task opens its PR off a unique throw-away branch
  (`bench/{scenario}/{8-hex-uuid}`) so multiple `[BENCHMARK]` PRs
  on the same source branch don't fight over the (from_branch,
  to_branch) uniqueness constraint. Cleanup: `__aexit__` declines
  the PR and deletes the temp branch; pre-session sweep walks
  stale `bench/*` branches in case the previous run was killed.

  Reuses the temp-branch infrastructure already on
  `code-review-benchmarks/feature/parallel-bench-temp-branches`
  (commit 3c7dd62) — that work isn't merged into master because
  it predates the gentle/aggressive lever; 5d is what actually
  motivates merging it.

**CLI surface.**
```
bench/cli.py run \
  -p deepseek -p qwen3 -p qwen3-6 \
  -t interaction -n 2 \
  --mode aggressive \
  --max-per-provider 2
```
- `--mode gentle | aggressive` (default `gentle`).
- `--max-per-provider N` (default 2 in aggressive, ignored in
  gentle). The bottleneck is the LLM endpoint, not the bench, so
  the budget is per-model — total in-flight ≤ N × providers.

**Where:** `benchmark/cli.py` (typer flags + asyncio fan-out),
`benchmark/bitbucket/real_factory.py` (already has temp-branch
build/close on the parallel branch).

**Trade-off to keep an eye on.** Aggressive mode amplifies any
shared-state quirk: Bitbucket SSL EOF blips, LLM endpoint rate
limits, local CPU when each task spawns its own diff-graph
subprocess. Hence the per-provider cap; default of 2 is
deliberately conservative. For the corp Bitbucket Server we
also need exponential-backoff retry on `ssl.SSLError` /
`ConnectionError` (already added to both `bench` and
`diffgraph/bitbucket.py`, env-tunable via
`BENCH_BB_RETRY_*` and `DIFFGRAPH_BB_RETRY_*`).

### 5d.2 Bench BranchUpdater — second-round PR refresh for SCEN-302

SCEN-302 ("agent reviews fix push after own prior comments") was
moved out of `drafts/` and lives in `java/` now, but it still
runs as a single-round review against the buggy `*_step0` branch
because the bench has no `BranchUpdater`. The scenario's
`setup.refs_update.target_ref` and `trigger.rounds` fields are
silently ignored by the loader, so round 2 never fires and the
forbidden_comments check (don't re-flag the null guard that the
fix already added) fails by construction.

To make the scenario meaningful:
1. Paired branches in the orderflow test repo:
   `hotfix/ORD-287-cancel-npe-step0` (current buggy state) and
   `hotfix/ORD-287-cancel-npe-step1` (step0 + the null-guard fix
   commit). Step1 must already exist in the mirror.
2. `benchmark/runner/branch_update.py` with two strategies:
   - `fast-forward`: push HEAD of `*_step1` onto the temp source
     branch via Bitbucket Server's branch-utils plugin.
   - `force-push`: alternative for when the new commit isn't a
     fast-forward.
3. Extend `ScenarioSetup` with `refs_update {after_round, strategy,
   target_ref}` and `TriggerSpec` with `rounds: int = 1`.
4. Multi-round runner loop:
   ```
   for r in range(rounds):
     post seed_comments (round 1 only)
     post trigger_comment(r)
     run agent
     capture_round_outputs
     if r+1 < rounds and refs_update.after_round == r+1:
         branch_updater.advance(refs_update)
   ```
5. Judge sees round-2 comments scored against expected_output,
   plus a `prior_round_comments` block in the prompt so it can
   verify the agent actually acknowledged what was already
   covered.
6. Per-round subdirectories under `attempt-NN/round-1/`,
   `round-2/`.

Out of scope until current review-quality issues are debugged
(SCEN-302's score=0/0.95 split across providers in the smoke
run is noise — the test isn't actually testing what it claims to
test until BranchUpdater exists).

### 5d.3 Reflect-based agent isolation as a general unit-test pattern

The reviewer's REV-001-concerns test landed on a clean general
pattern that generalises to any agent with `reflect` in @tools:

1. **Custom user message** ("--user-message-from") tells the agent
   to do its analysis phase only — write findings/concerns/
   conclusions into `reflect(...)` and immediately call `done()`
   with empty output. Explicit "do NOT spawn/post/set_status" rules.
2. **No mocks needed** — agent never calls action tools, so there's
   nothing to mock. (A defensive empty-spawn mock can absorb a
   stray spawn if the agent ignores the instruction; not required.)
3. **Judge reads invocations.json** for `reflect(...)` args, treats
   them as the test signal. Pairs with the existing reading of
   `done(findings)` and `spawn_agent.focus`.
4. **Expected output** is a list of keyword groups (existing
   `concern_focuses`/`description_keywords` infra), AND-of-OR match
   against title + description fields of each reflect entry.

Coverage matrix today:

| Agent        | Has reflect? | Status |
|--------------|--------------|--------|
| reviewer     | yes          | REV-001-concerns ✅ landed |
| investigator | yes          | INV-002 (todo) — same shape: focus + AGENTS.md citations + questions_remaining via `reflect(learned, questions_remaining)` |
| dispatcher   | no           | Add `reflect` to dispatcher's @tools if we want symmetric isolation, OR skip — dispatcher is a router with fewer "thinking" phases. Decide later. |

Why this matters:
- Decouples LLM-provider quirks (parallel tool calls, tool-parser
  divergence) from unit-test stability — agent never reaches the
  parallel-spawn step.
- Decouples mock-fixture maintenance from test correctness — there
  IS no fixture to keep coherent with the agent's actual focuses
  ("mocked investigator returns finding for concern A even when
  reviewer asked about concern B" is structurally impossible).
- Cheapest LLM-cost shape for unit tests: read diff (1-3 LLM
  calls) + reflect (1) + done (1). ~5 calls vs 20-50 for full
  pipeline.
- Lets us interrogate richer cognitive signal: reflect carries
  `learned` (facts), `questions_remaining` (gaps), `confidence`
  — testable separately. E.g. an investigator unit test can
  assert "agent identified the right gap" by matching against
  `questions_remaining`, not just `learned`.

**Concrete next step:** INV-002-investigation-only as a mirror of
REV-001-concerns. Same custom-user-message pattern, focus is
`PRICING LOGIC: selectFreeItem returns get(0)`, expected
`concern_focuses` checks that the investigator's reflect
`learned` includes "cheapest" / "AGENTS.md" / "first item" and
that `questions_remaining` is empty (or has the right gaps).

**Stretch:** if dispatcher gets reflect, write DISP-002 measuring
how the dispatcher classifies trigger messages without acting
on them ("/review → would spawn reviewer", "/help → would
explain commands", plain text → "would treat as /ask"). Useful
for testing dispatcher routing logic without spinning up the
spawned agents.

#### Specialised subagents as extension points

Subagents are first-class extension points. The reviewer's system
prompt frames `spawn_agent` as a capability ("delegate depth to
investigators when the user message asks for it"); it doesn't
hardcode that there's one general investigator. orchestra's
`AgentRegistry` already lets us add specialised investigators by
dropping new `.md` files into `diffgraph/prompts/`:

```
diffgraph/prompts/
  reviewer.md
  investigator.md                    — general (what we have today)
  security-investigator.md           — authz, IDOR, SQL injection, secrets
  performance-investigator.md        — N+1, indexes, connection pooling
  agents-md-investigator.md          — narrow project-conventions audit
  testing-investigator.md            — test coverage, mocks, flakiness
  threading-investigator.md          — race conditions, atomicity, locks
```

Reviewer's system gets one extra paragraph:

> "When you have a concern, call `list_agents()` first to see who
> can investigate it best. Pick the most specialised investigator
> whose summary matches the concern's nature; fall back to the
> generic `investigator` if none fits."

Zero changes to the reviewer between adding specialists — true
Open/Closed. Each new investigator is self-contained: own system
(specific methodology), own user template (concern + diff), own
budget tuned to its scope.

User-message can also be used as an **ad-hoc constraint**:
"for this run, only `security-investigator` is allowed". Useful
for thematic audits — "run a security-only sweep of all open
PRs", "weekly performance audit", etc.

Path: ship REV-001 / INV-001 stable on the general investigator
first, then a specialist (`agents-md-investigator` is the
narrowest and most testable starter — its job is just "does the
diff respect AGENTS.md rules", which we already test for in the
existing benchmark scenarios). Adding it should improve scores
on SCEN-009 / SCEN-010 / SCEN-305 because the reviewer can
explicitly delegate convention-checking to a focused agent
instead of relying on the general investigator to remember.

`list_agents()` is already implemented — works with both real and
mocked spawns (the mock matches by `agent` name in args).

#### Open question — single reflect vs full chain vs new "outcome"

The current REV-001 implementation reads ALL reflect calls from
invocations.json and matches keyword groups against the union of
title+description across them. That works for concerns-forming
because each concern is a self-contained line of inquiry.

For richer agents (investigator especially), a single reflect
entry is a snapshot of an internal step — `learned: "still
checking X"` only makes sense inside the trajectory of the
preceding reflects. Reading the LAST reflect alone may miss
context; reading ALL reflects is verbose and the LLM judge has
to reason over the full reasoning chain.

Three design choices, decide before scaling 5d.3 to investigator
and dispatcher:

(a) **Full chain as the test signal** (current REV-001 behaviour).
   Judge sees every reflect; expected matchers run against the
   union. Pros: nothing new in the framework, all the agent's
   thinking is visible. Cons: judge prompt grows linearly with
   reasoning depth; brittle if the same keyword appears in an
   intermediate reflect that later got resolved.

(b) **Last reflect is the contract**. Mandate that the agent's
   last `reflect` before `done()` carries a self-contained
   summary in `learned` (and maybe other fields). Judge reads
   only that one. Pros: clear single signal; small judge prompt.
   Cons: changes the implicit contract of `reflect` (today every
   call is just a thinking checkpoint, not a "final summary"
   slot); easy to forget in agent prompts.

(c) **New self-contained `outcome` tool**. A dedicated step at
   the end of any agent run: `outcome(summary, findings,
   verdict, evidence)`. Self-contained, explicit, parallel to
   `done` but for the "what I concluded" channel rather than
   the "what I published" channel. Judge reads the single
   `outcome` call. Pros: clean contract; works regardless of
   how many reflects happened or in what shape; gives tests
   AND parent agents (in production) a stable handoff surface.
   Cons: another tool to teach in every agent's @tools list;
   risk of overlap with `done(findings=...)`.

Lean toward (c) — feels most honest about what we're testing
("the agent's final conclusion as it would deliver it to a
human or a parent agent"), and removes the ambiguity in
`done(findings=...)` between "publish these" and "I think
these are true". But it's a real contract change, so we ought
to land INV-002 with (a) first to see how brittle the full-chain
approach actually is in practice before introducing a new tool.

If we go with (c), `done(findings=...)` keeps its publish-only
semantics and the parent reads `outcome` for the conclusion
shape — including in production, where the reviewer reading the
investigator's `outcome` is more natural than rummaging through
`done.findings`.

### 5d.1 Live, progressive run dashboard (per-provider throughput)

When `bench run --mode aggressive` is in flight the user is
flying blind: verdict lines only print after `asyncio.gather`
completes (the cycle was rewritten so that aggregation is
deterministic regardless of completion order, but that traded
away the per-task progress feed gentle mode used to give). For
a 50+ task run that's 15–30 minutes of silence. Need a small
live dashboard that updates as tasks finish and surfaces per-model
throughput in real time.

**Layout** (refreshed every ~2s while the run is active):

```
Aggressive run · max-per-provider=2 · 38/54 done · ETA 4m
─────────────────────────────────────────────────────────────
provider       in_tok/s  out_tok/s  cache%   p50_lat  in-flight
deepseek            220        6.8   77.6%      2.8s     2/2  ▓▓
qwen3-coder         107        3.9   23.8%      5.3s     1/2  ▓
qwen3-6 (g1nft)     736       28.6    0.0%      1.1s     2/2  ▓▓
─────────────────────────────────────────────────────────────
recent verdicts (last 6):
✅ qwen3-6      SCEN-204     score=0.97   18s
✅ deepseek     SCEN-205b    score=1.00   24s
⚠️  qwen3       SCEN-009     score=0.62   145s
…
```

**Computed from** the per-LLM-call usage already in the trace
DB (`agent_llm_response.usage`): pair `agent_llm_request` ↔
`agent_llm_response` by `(run_id, agent_id, step)`, derive
duration; `prompt_tokens` is the *full* input (cached + paid)
on purpose — a model with prefix caching SHOULD look faster
because that's the user-perceived throughput. `cached_tokens /
prompt_tokens` shows how much of that speed is cache.

**What it answers:**
- Is one provider stalling while others race? (in-flight column).
- Why is one provider behind on score — slow LLM, or actually
  worse answers? (latency vs verdict feed side by side).
- Is the cache really helping me, or am I paying full input
  tokens? (cache% column).

**Cheap path:** a `rich.live.Live` table updated from a
background asyncio task; throughput sampled from the trace DB
in 5–10s windows so noise smooths out. Keep it inline in
`bench/cli.py run`; no separate command. Optional flag
`--no-dashboard` for log-friendly CI runs.

**Stretch:** export the same metrics as JSON for a future
external dashboard; tag each window with the prompt hash so
generation-over-generation throughput trends are queryable.

---

## 5e. Quality API server — DB-backed state, web UI + CLI clients

> Productionisation layer for §5c: the QC pipeline becomes a
> long-running server with DB state (so it survives restarts), a
> single API surface, a web UI for navigation, and a thin CLI
> client. Replaces the §5c sketch where state lived in
> `qa-state.json` + `queue-<ts>.jsonl` files and `bench-schedule`
> did the work itself. Aligns the QC pipeline with the rest of the
> ecosystem (one FastAPI app, one DB, cross-linkable URLs between
> traces, runs, plans, and scenarios).

### 5e.1 Server consolidation: pr-analytics → quality-api

Today three FastAPI surfaces exist in parallel:
- `webhook/` — incoming Bitbucket events (reactive, stays separate;
  it has a different lifecycle than a control-plane).
- `tracing/` (TODO §4.1) — trace browser, runs list, live event WS.
- `pr-analytics/` — production review-quality metrics dashboard.

Adding a fourth `qa-api/` would split the control-plane further.
**Fold trace browser + QA scheduler into pr-analytics**, renaming
the package conceptually to `quality-api`. One process, one DB
(SQLite now, Postgres when contention demands), one deploy. Three
route packs in one app:

```
quality-api  (was pr-analytics)
├── /prod/*         — production metrics (existing)
├── /traces/*       — trace browser (move from diff-graph/tracing/)
└── /qa/*           — QC scheduler (new)
```

Cross-link benefits:
- run page → trace page → specific agent step (single URL space).
- mutation page → "regression in scenario X since this commit".
- scenario page → all past runs across all branches/mutations.

Webhook keeps its own lifecycle. Diff-graph CLI stays (the bench
worker invokes it as a subprocess).

### 5e.2 DB-backed state model (replaces qa-state.json)

```sql
qa_branches
  branch              TEXT PK
  last_qc_commit      TEXT
  last_seen           DATETIME
  state               TEXT       -- new | qc_running | qc_ready | qc_failed | superseded

qa_plans              -- one row per "plan a QC matrix" call
  id                  INTEGER PK
  created_at          DATETIME
  created_by          TEXT       -- user / cron / api / webhook
  branches            JSON       -- list of (branch, sha)
  providers           JSON
  scenarios           JSON
  attempts_min        INTEGER
  state               TEXT       -- queued | running | done | cancelled
  cancel_reason       TEXT

qa_tasks              -- one row per (plan, branch, sha, provider, scenario, attempt_n)
  id                  INTEGER PK
  plan_id             INTEGER FK
  branch              TEXT
  mutation_hash       TEXT
  provider            TEXT
  scenario            TEXT
  attempt_n           INTEGER
  state               TEXT       -- queued | leased | running | finished | error | cancelled
  priority            INTEGER    -- lower = sooner; sentinel/cheap first
  lease_owner         TEXT       -- worker id; NULL when queued
  lease_expires_at    DATETIME   -- heartbeat-driven
  enqueued_at         DATETIME
  started_at          DATETIME
  finished_at         DATETIME
  trace_run_id        TEXT       -- FK to traces.runs
  result_json         JSON       -- judge verdict + scores
  error_class         TEXT       -- timeout | api | bench-error | NULL

qa_runs               -- finished outcomes (= §5c's agent_qa_runs renamed)
  -- hs_*, co_*, agent_warnings_count, etc. as in §5c.499

qa_workers            -- liveness
  id                  TEXT PK    -- uuid + hostname
  pid                 INTEGER
  provider            TEXT       -- which LLM endpoint this worker serves
  capacity            INTEGER    -- max in-flight tasks
  started_at          DATETIME
  last_heartbeat      DATETIME
  state               TEXT       -- running | idle | dead

qa_mutations          -- (future, §5c.609) explicit prompt-version entity
  hash                TEXT PK
  parent_a            TEXT
  parent_b            TEXT       -- NULL unless cross-bred
  prompt_blob         JSON       -- compiled prompt set
  source              TEXT       -- manual | evolved | merged
  created_at          DATETIME
```

**Restart recovery.** On server start, a reaper:
1. `UPDATE qa_tasks SET state='queued', lease_owner=NULL
    WHERE state='leased' AND lease_expires_at < NOW()` —
    abandoned leases return to the queue.
2. `UPDATE qa_workers SET state='dead'
    WHERE last_heartbeat < NOW() - 60s`.
3. Scans `qa_plans WHERE state='running'` to recompute progress
    (no plan-level lock state to recover; tasks are the unit).

No "in-flight queue file" to lose, no "did we already start this
plan" ambiguity — the DB IS the truth.

### 5e.3 API surface

```
# Discovery / planning
POST /qa/discover                       — git fetch + diff vs qa_branches
GET  /qa/branches                       — table view, filter by state
POST /qa/plans                          — create plan {branches, providers, scenarios, attempts_min}
GET  /qa/plans?state=&since=            — list with pagination
GET  /qa/plans/{id}                     — one + summary (done / total / pass rate)
POST /qa/plans/{id}/cancel              — soft cancel (running tasks finish, queued drop)

# Execution (worker contract)
POST /qa/tasks/lease?provider=X         — atomic SELECT…LIMIT 1 + UPDATE state='leased'
POST /qa/tasks/{id}/heartbeat           — extend lease_expires_at
POST /qa/tasks/{id}/finish              — submit {state, result_json, trace_run_id, error_class?}
POST /qa/tasks/{id}/cancel              — abort an in-flight task

# Browse
GET  /qa/tasks?state=&plan=&branch=     — list
GET  /qa/tasks/{id}                     — one + result + trace link

# Outcomes / dashboards
GET  /qa/runs?branch=&mutation=         — finished runs, filterable
GET  /qa/runs/{id}                      — one
GET  /qa/dashboards/branches            — per-branch deploy-ready bool
GET  /qa/dashboards/mutations           — per-(branch, mutation_hash) scoreboard
GET  /qa/dashboards/scenarios           — per-scenario discriminative power
                                          (variance across mutations — sentinel calibration)

# Cross-domain
GET  /scenarios                         — all known scenarios (read from bench repo)
GET  /scenarios/{id}                    — one + run history timeline
GET  /traces/{run_id}                   — full trace (existing trace browser route)

# Live (WebSocket)
WS   /qa/live/tasks                     — stream task state-transitions (low-volume)
WS   /qa/live/tasks/{id}                — per-task event tail (high-volume:
                                          agent_step, tool_request/result, reflect, judge)
WS   /qa/live/plans/{id}                — per-plan progress counters
```

The lease contract is the one piece that has to be transactional:
`SELECT … FOR UPDATE LIMIT 1` (or SQLite's BEGIN IMMEDIATE) + the
status flip happen in one transaction. Workers can crash safely;
reaper recovers leased-but-dead tasks back to queued.

### 5e.4 Worker model

`bench-schedule worker --provider qwen3-6 --capacity 2`:

```
register POST /qa/workers (gets a worker_id)
loop:
    task = POST /qa/tasks/lease?provider=qwen3-6
    if not task: sleep(3); continue
    spawn heartbeat thread (every 15s, lease 60s)
    try:
        invoke benchmark/cli.py run -s <scenario> -p <provider> ... 
                     -- in a per-task git worktree (no collisions)
        result = parse_result_json + parse_invocations + parse_judge
        POST /qa/tasks/{id}/finish {state='finished', result, trace_run_id}
    except Exception as e:
        POST /qa/tasks/{id}/finish {state='error', error_class=type(e).__name__}
    cleanup worktree
```

Workers are stateless over the API. Multi-machine, multi-provider
just works. The server doesn't track in-flight in memory — only
through DB state + heartbeat.

Gentle vs aggressive in this model:
- `bench-schedule worker --provider qwen3-6 --capacity 1` =
  gentle (one task at a time per provider).
- `bench-schedule worker --provider qwen3-6 --capacity 4` =
  aggressive.
- A central token bucket lives server-side
  (`qa_provider_rate_limits` table) and the lease query honours
  it. So even with capacity=N the server won't hand out more than
  the rate-limited budget.

### 5e.5 CLI client (thin, talks to API)

```
bench-schedule discover                                        → POST /qa/discover
bench-schedule plan --branches feature/X,feature/Y             → POST /qa/plans
bench-schedule plan --auto-discover-since master               → /qa/discover then /qa/plans
bench-schedule worker --provider qwen3-6 --capacity 2          → polls /qa/tasks/lease
bench-schedule status                                          → GET /qa/dashboards/branches
bench-schedule report --plan 42                                → GET /qa/plans/42 (markdown)
bench-schedule watch 42                                        → WS /qa/live/plans/42 (rich live)
bench-schedule logs <task-id>                                  → WS /qa/live/tasks/<id>
```

No local DB, no `qa-state.json`, no `queue-*.jsonl`. Single config
file holds only `--server URL` + auth token. The CLI is just a
shell over HTTP.

### 5e.6 Web UI (additive in pr-analytics, FastAPI + HTMX/Alpine)

```
/qa/                            — landing: live counts + recent plans
/qa/branches/                   — branch | last_commit | state | pass_rate | last_run
/qa/plans/                      — plan_id | created_by | branches | progress | state
/qa/plans/{id}                  — drill: matrix (provider × scenario), per-cell pass/fail
                                  + live progress bar + cancel button
/qa/tasks/{id}                  — drill: lineage + live event feed
                                  + ↗ trace, ↗ scenario, ↗ run, ↗ plan
/qa/runs/{id}                   — drill: judge verdict, agent_warnings, side-effects
                                  + ↗ trace, ↗ plan, ↗ mutation
/qa/scenarios/                  — list + discrimination power
/qa/scenarios/{id}              — history timeline
/qa/dashboards/mutations        — per-mutation scoreboard, deploy-ready badge
/qa/dashboards/calibrate        — sentinel candidate variance plot

/traces/                        — existing trace browser (moved here)
/traces/{run_id}                — existing trace UI
/prod/                          — existing pr-analytics dashboard
```

Cross-links between routes (run ↔ trace ↔ plan ↔ scenario ↔
mutation) are the main UX: one URL space, one navigation graph.

Each WS route emits standard JSON events; same payload shape as
`agent_step` / `agent_tool_*` records that already land in trace
DB. The web UI subscribes per-task to render a live progress card:

```
┌─ Task #1234 — REV-001 / qwen3-6 / attempt 1 ──────────────┐
│ status: running   step 14/50   tokens: 12.4k / 50k        │
│ ─────────────────────────────────────────────────────────  │
│ ▸ step 12  read_file("PricingService.java", changes_only)  │
│ ▸ step 13  read_outline("Order.java")                       │
│ ▼ step 14  reflect()  ← in flight                           │
│   learned: …                                                │
│   questions_remaining: [Q1, Q2, …]                          │
└────────────────────────────────────────────────────────────┘
                                            [↗ open full trace]
```

### 5e.7 Live observation pipe (server-side fan-out)

The bench worker writes events to `~/.diffgraph/traces.db` exactly
as it does today (no change to the agent path). The server tails
events for currently-leased tasks and republishes them on
`/qa/live/tasks/{id}`:

- v1 (SQLite): `SELECT … WHERE id > $cursor` polled every 500ms
  per active subscriber. Fine for ~10s of concurrent watchers.
- v2 (when we move to PG): `LISTEN/NOTIFY` channel per run_id.
- v3 (if scale demands): worker emits to a pub/sub side-channel
  (Redis Streams) and the server forwards to WS subscribers
  without going through DB at all.

Don't over-engineer v1; the agent already writes incrementally so
polling is genuinely fine for our load.

### 5e.8 Server topology nuances

A. **SQLite vs Postgres.** Stay on SQLite until contention bites
   (probably >5 concurrent workers writing tasks). PG migration is
   mechanical; do it when measured, not preemptively. WAL mode +
   `PRAGMA busy_timeout = 5000` is enough for now.

B. **Auth.** Even local — once we have a web UI and multi-worker,
   we need at least token-based auth on `/qa/tasks/*`. Simple env:
   `QA_WORKER_TOKEN` + `QA_ADMIN_TOKEN`. Browser session is
   cookie-based. Don't ship without this.

C. **Where scheduler policy lives.** The "which task goes next"
   decision lives in the lease query, not in a separate planner
   process. Policy = `ORDER BY priority, enqueued_at LIMIT 1`
   plus a WHERE clause for rate budget / fairness. Adding new
   priorities is a SQL change, no new component.

D. **Webhook integration (later).** `/qa/discover` can be wired
   into the Bitbucket webhook so a push to `feature/*` auto-
   creates a plan. Connects reactive event flow to the QC matrix.
   Easy add-on; don't put it in MVP.

E. **Mutation tracking.** Today mutation = SHA. If §5c.609
   evolution lands, mutations come from semantic merges, not
   commits. Add `qa_mutations` (above) when that's real; not now.

F. **Bench worker location.** Workers can be co-located with the
   server (single host, simplest) or run on a remote machine
   (e.g. one with the LLM-ssh tunnel set up). API is HTTP, so it
   doesn't matter. Document `--server` config explicitly so this
   stays decoupled.

### 5e.9 Migration from §5c sketch

Drop the `qa-state.json` / `queue-*.jsonl` proposal entirely; it
was always going to be a stop-gap. The original §5c MVP order
(steps 1-8) reshuffles to:

1. Move `tracing/` FastAPI app under `pr-analytics/` (mechanical,
   no behaviour change).
2. Add DB schema (5e.2) + migration script.
3. Implement `/qa/discover`, `/qa/plans` (POST/GET), `/qa/tasks`
   list + lease + heartbeat + finish.
4. Rewrite `bench-schedule` as API client (no local state).
5. Build the worker loop; verify gentle mode end-to-end on one
   provider.
6. Add metrics/result computation in `/qa/tasks/{id}/finish` —
   compute hs_/co_ scores from the judge response, write
   `qa_runs` row.
7. Dashboards: branches, mutations, scenarios. Read-only HTML
   first, progressive enhancement to live updates.
8. Live WS routes: plan-level first (low volume), then task-level.
9. Sentinel calibration page (variance plot from `qa_runs`).
10. Aggressive mode: per-provider rate limits + multi-worker.
11. Webhook auto-discover (5e.8.D).
12. (Future) §5c.609 evolution loop on top.

### 5e.10a Two trace storages — SQLite + filesystem tree, unified for agents AND judges

Both LLM-driven workloads in our pipeline — **agents** (dispatcher /
reviewer / investigator) and **judges** (bench scoring + future
evaluators / debuggers) — write to the **same dual-storage scheme**.
A "run" is any sequence of LLM calls; the storage layer doesn't
care whether the LLM was driving an agentic ReAct loop or a
single-shot judge verdict. Today the two are split (agent → SQLite +
fs tree; judge → fs tree only), which means dashboards / live UI
can't see judge calls, and joins like "show me runs where the agent
said APPROVED but the judge said NEEDS_WORK" require ad-hoc fs
scraping. Unify.

**Storage role split:**

1. **SQLite** `~/.diffgraph/traces.db`
   - Schema: `runs` + `events`. Append-only event log per run.
   - **Unify:** add a `kind` column to `runs` —
     `kind IN ('agent', 'judge', 'evaluator')`. `agent_step`,
     `agent_tool_request`, `agent_tool_response`, `agent_reflect`,
     `agent_done` events keep their names; add parallel
     `judge_request`, `judge_response`, `judge_verdict` events
     for judge runs. Same `run_id` namespace, same `events`
     table — just a different `kind` filter when querying.
   - A bench attempt then has TWO `runs` rows: one
     `kind='agent'` (the diff-graph subprocess) and one
     `kind='judge'` (the LLMJudge call). Linked via
     `qa_tasks.agent_run_id` + `qa_tasks.judge_run_id`.
   - Purpose: **fast queries**. List recent runs, sort by tags,
     join with `qa_runs`, count tool calls per type, detect
     stuck steps via timestamp gaps, render the live progress
     feed. Same queries work for both agent and judge runs;
     dashboards can mix them ("show me judge calls > 30s on
     deepseek over the last week").
   - Always written.
   - Indexed by `run_id`, `started_at`, `pr_url`, `prompt_hash`,
     `kind`.

2. **Filesystem tree** under `BENCHMARK_TRACE_DIR` /
   `DIFFGRAPH_TRACE_PATH`
   - **Unify the layout.** Today it's asymmetric — agent runs
     live under `agent/agents/<sub_agent>/step-NN-…json` while
     judge runs are special-cased as a flat `judge/request.json`
     + `judge/response.json` pair. We want **homogeneous** layout
     so the same tooling (read_file walks, tree views, agentic
     deep-dive) works for both:

     ```
     <attempt-dir>/
     ├── runs/
     │   ├── agent-<run_id>/
     │   │   ├── meta.json
     │   │   ├── events.jsonl
     │   │   └── agents/<sub_agent>/
     │   │       ├── step-00-request.json
     │   │       ├── step-00-response.json
     │   │       ├── step-00-tool-01-request.json
     │   │       ├── step-00-tool-01-response.json
     │   │       └── …
     │   └── judge-<run_id>/
     │       ├── meta.json
     │       ├── events.jsonl
     │       └── agents/judge-0/
     │           ├── step-00-request.json     # single-shot: just one step
     │           └── step-00-response.json
     ├── invocations.json                     # bench-side, agent's tool log
     └── result.json                          # final verdict + score
     ```

     Same scaffolding for both. Single-shot judges still get the
     full request/response/meta/events.jsonl stack — just with
     one step. Multi-shot judges (when we have evaluators that
     reflect / call tools) slot in unchanged. Future
     `kind='evaluator'` runs (debugger sub-agent that reads
     traces, see §9.7) reuse the same shape.

   - Purpose: **full-fidelity inspection**. Open one specific
     LLM request as JSON (whole system prompt + messages + tools
     schema) when debugging a drift; let an agent (or human) do
     `read_file` over the tree to deep-dive a specific run. The
     tree is structured for easy agentic walking: paths are
     predictable, files are small enough to read directly,
     hierarchy mirrors the call graph regardless of agent vs
     judge.
   - Written only when env / flag enables it.

The two are **complementary**, not redundant:

| Use case | SQLite | Filesystem |
|---|---|---|
| List runs | ✓ | — (no index) |
| Live progress feed | ✓ (poll) | — (would need fs.watch) |
| Joins with QA metrics | ✓ | — |
| Read full step-12 LLM request body | partial (truncated in events) | ✓ (full JSON) |
| Agentic deep-dive ("read the trace, find the drift") | hard (needs SQL) | ✓ (just `read_file`) |
| Crash safety | ✓ (per-event commit) | ✓ (per-step write) |

**Quality-api integration:**

- Server **always** reads SQLite for indices, lists, dashboards,
  live WS streams. Same code path for `kind='agent'` and
  `kind='judge'` rows — they're just runs.
- Server **records the filesystem path** alongside each run
  (`qa_tasks.agent_fs_trace_path` + `qa_tasks.judge_fs_trace_path`)
  so every run page has "↗ Filesystem trace" links — both for
  the agent and for its scoring judge — with absolute paths the
  human (or another agent) can `cd` into and `ls` directly.
- The web UI offers two views per run, identically for agent and
  judge:
  - **Indexed view** (`/traces/{run_id}`) — fast browser,
    SQLite-backed. Header shows `kind` + cross-link to its
    counterpart (agent run → judge run that scored it; judge run
    → agent run it scored).
  - **Tree view** (`/traces/{run_id}/files`) — file-explorer over
    the filesystem trace tree, server reads files on demand and
    renders raw JSON / pretty diff. Useful when the indexed view
    truncated something.
- Agentic deep-dive workflow: a meta-agent (debugger / evaluator)
  gets the absolute path of either an agent run OR a judge run
  via the same `read_file`/`list_files` tools — same scaffolding
  in both, no special-casing. Same shape as our DiffSearch
  sibling mounts in §9; could be auto-mounted under
  `workspace/traces/<run_id>/` for a debugging sub-agent. The
  evaluator can then ask "did the agent's reflect at step 14
  match what the judge actually scored?" by reading both trees
  side-by-side.

**Retention.**
- SQLite: long retention (we want trends across mutations);
  prune events older than ~6m if size becomes a problem.
- Filesystem: shorter retention by default (it's bigger). One
  policy per source:
  - bench's `BENCHMARK_TRACE_DIR` — keep last N sessions, GC
    older.
  - production webhook traces — keep 30 days, GC older.
  - QA pipeline traces — keep all that match a `qa_runs` row,
    GC orphans.

**Disk-space failure mode.** If the filesystem trace path is
unwritable (disk full, permissions), the agent must NOT fail —
SQLite is the source of truth, fs trace is best-effort. Already
the case in `cli.py`'s trace writer; document it explicitly so
nobody changes it.

**Schema columns to add to `qa_tasks` / `qa_runs`:**
```
agent_run_id          TEXT   -- FK to traces.runs (kind='agent')
agent_fs_trace_path   TEXT   -- abs path to runs/agent-<id>/ tree, or NULL
judge_run_id          TEXT   -- FK to traces.runs (kind='judge')
judge_fs_trace_path   TEXT   -- abs path to runs/judge-<id>/ tree, or NULL
```

Both agent and judge get equal first-class treatment. When the
server returns a task / run JSON, it includes both paths so:
- `bench-schedule logs <task-id> --files` opens the agent tree.
- `bench-schedule logs <task-id> --files --judge` opens the judge
  tree.
- Web UI's run page links both.
- A debugging sub-agent receives both as workspace mounts.

**Migration of the writer side:**
- Diff-graph CLI: already writes both SQLite and the agent
  filesystem tree. No change.
- Bench `LLMJudge`: today writes only `judge/request.json` +
  `judge/response.json` flat. Migrate to:
  1. Open a `runs.runs` row with `kind='judge'`, get a
     `run_id`.
  2. Write events (`judge_request`, `judge_response`,
     `judge_verdict`) to `events`.
  3. Mirror to `runs/judge-<run_id>/agents/judge-0/step-00-…`
     using the same writer the agent already uses (refactor
     the writer to be kind-agnostic — same code, different
     `runs/<kind>-<id>/` parent).
  4. Drop the old flat `judge/{request,response}.json` files
     once readers (judge result parser, dashboards) are
     migrated.
- New `kind='evaluator'` slots in by reusing the same writer,
  no new code needed.

### 5e.11 Search & query API — debug-driven design

Built on top of the storage abstraction (5e.10a). The API serves
debugging needs we've actually hit, organised in four layers.

**Storage roles, finalised.**
- **SQLite is primary.** All search, list, aggregate, longest-runs,
  outlier queries — driven by SQL. Every run record carries an
  `fs_trace_path` so the API can always offer a "↗ filesystem
  trace" link from any DB row. **DB to FS is one-way navigation:
  DB → file paths**, not the other way around. The FS tree alone
  doesn't power /api/runs — you'd have to walk every directory to
  find one outlier.
- **FS complements, doesn't replace.** Use cases: open one
  specific LLM request body in full fidelity, agentic deep-dive
  via `read_file` over the tree, bring a session dir from a
  remote machine, archive long-term. The FS-only trace browser is
  a separate thin app, useful for ad-hoc dumps but not the
  primary source of truth.

**Five dimensions of search, distilled from real debugging.**

```
A. by RUN attributes:
   kind, agent_name, model, status, started_at, duration_ms,
   tokens, prompt_hash (mutation), prompt_source (generation)

B. by EVOLUTIONARY identity (genes / mutations / generations):
   ?generation=prompts-experimental
   ?mutation=abc1234                  # short hash
   ?gene=diff_view_block              # AND when repeated
   ?gene_any=phase_gating|bulk_post   # OR
   ?without_gene=baked_existing_comments

C. by WORK OBJECT (what was reviewed/touched):
   ?pr_url=https://...
   ?project=SBLOOM
   ?file=PricingService.java          # one of files_touched
   ?jira=ORD-234                      # one of jira_keys
   ?scenario=DISP-002                 # bench scenario id
   ?scenario_tag=tier:unit

D. by ACTIVITY (what the agent did):
   ?has_tool=reflect                  # called this tool at least once
   ?has_event=agent_forced_done       # this event fired
   ?tool=read_file                    # used in /tool_calls endpoint
   ?args_path=$.confidence&args_value=high
   ?response_size_gt=2000

E. by RELATIONSHIP:
   ?linked_run=<id>                   # paired agent ↔ judge
   ?same_scenario_as=<id>             # same scenario, different mutation
   ?has_finding_severity=BLOCKER
```

**Endpoint surface (refines 5e.3):**

```
# Layer 1: list runs
GET /api/runs                         # all five dimensions as filters
GET /api/runs/{id}                    # full run + linked agent/judge ↔ counterpart
GET /api/runs/{id}/events             # paginated, filterable by tool/type
GET /api/runs/{id}/steps              # step-level summary (count + duration per step)
GET /api/runs/{id}/steps/{n}          # all events in one step
GET /api/runs/{id}/tool_summary       # per-tool count/avg-duration for this run
GET /api/runs/{id}/timeline           # ascii timeline for CLI rendering
GET /api/runs/{id}/files              # links into FS tree for this run

# Layer 2: cross-run search (the most-used family in practice)
GET /api/tool_calls                   # "show me reflect examples on qwen3-6"
GET /api/events                       # raw event search when tool_calls isn't enough
GET /api/findings                     # search through agent's emitted findings
GET /api/comments                     # search through agent's post_comments

# Layer 3: catalogues / discovery
GET /api/scenarios                    # all bench scenarios + run history per
GET /api/scenarios/{id}               # drill: history + median per (provider, mutation)
GET /api/genes                        # gene catalogue + runs/mutations counts
GET /api/genes/{name}                 # one gene + perf delta with vs without
GET /api/mutations                    # all known mutations
GET /api/mutations/{hash}             # one mutation + manifest + parent links
GET /api/work_objects                 # file/pr/jira/project/scenario keys touched
GET /api/work_objects/{type}/{key}/runs   # runs touching this object

# Layer 4: aggregates / regressions
GET /api/aggregates/by_tool           # tool usage stats
GET /api/aggregates/by_scenario       # per-scenario perf
GET /api/aggregates/by_provider       # per-provider perf
GET /api/aggregates/by_gene           # per-gene perf delta — substrate for evolution
GET /api/regressions                  # baseline_hash vs candidate_hash
GET /api/comparisons                  # arbitrary dimension cross-cut

# Live (WebSocket)
WS  /api/live/runs                    # task state-transitions
WS  /api/live/runs/{id}               # per-run event tail
WS  /api/live/plans/{id}              # per-plan progress
```

**Schema additions (denormalised for query speed):**

```sql
runs (extends 5e.10a):
  agent_name      TEXT     -- root agent: dispatcher | reviewer | investigator | judge
  generation      TEXT     -- prompt source identifier (e.g. "prompts-experimental")
  mutation        TEXT     -- alias of prompt_hash; mutation hash
  genes           TEXT     -- JSON array of gene names active in this mutation
  project         TEXT     -- bitbucket project, extracted from pr_url
  files_touched   TEXT     -- JSON array, extracted from diff_summary
  jira_keys       TEXT     -- JSON array, extracted from PR description + comments
  scenario_id     TEXT     -- bench scenario id, NULL outside bench
  scenario_tags   TEXT     -- JSON array, e.g. ["tier:unit", "agent:dispatcher"]
  linked_run_id   TEXT     -- pair: agent_run ↔ judge_run for the same attempt
  duration_ms     INTEGER  -- finished_at - started_at, denormalised for sort
  fs_trace_path   TEXT     -- absolute path to runs/<kind>-<id>/ tree, or NULL

mutations:                  -- new table
  hash            TEXT PK
  generation      TEXT
  manifest        TEXT     -- JSON {gene: on/off, ...}; NULL for commit-based mutations
  kind            TEXT     -- 'commit' | 'toggle'
  parent_a        TEXT
  parent_b        TEXT
  detected_at     DATETIME
  created_by      TEXT     -- 'compiler' | 'manual' | 'evolution' | 'merge'

INDEX idx_runs_kind_started   ON runs(kind, started_at DESC)
INDEX idx_runs_mutation       ON runs(mutation)
INDEX idx_runs_scenario       ON runs(scenario_id)
INDEX idx_runs_project        ON runs(project)
-- Gene/file/jira queries use json_each:
--   SELECT … FROM runs, json_each(runs.genes) WHERE json_each.value = ?
-- FTS5 on events.data_json — defer until perf shows it's needed.
```

### 5e.12 Genes — auto-detected now, toggle-driven later

Genes are discrete features inside a mutation. Today: **auto-
detected at compile time** from prompt content via marker
patterns. Tomorrow: **explicit toggles** in a manifest, with
prompts composed from base + gene patches. Both regimes share the
same query API.

**Auto-detection (Phase 1, current proposal):**

```python
# orchestra/genes.py
GENES = {
    "diff_view_block":            "## Diff view (how the file tools work)",
    "agents_md_citation_rule":    "Cite the rule by name when it bears",
    "severity_calibration_v2":    "follows consequence; verdict follows severity",
    "comment_graph_tools":        "list_threads(start, n, sort)",
    "no_baked_existing_comments": lambda agent: "existing_comments" not in agent.input_schema,
    "open_closed_user_message":   "The tools above are **capabilities**",
    # ...
}

# at compile time (orchestra/compiler.py):
def detect_genes(prompts: AgentRegistry) -> list[str]:
    out = []
    for name, marker in GENES.items():
        if callable(marker):
            if any(marker(a) for a in prompts.values()):
                out.append(name)
        else:
            if any(marker in (a.system_prompt + a.user_prompt) for a in prompts.values()):
                out.append(name)
    return sorted(out)
```

CLI passes `genes` to TraceDBWriter at run start. They land in
`runs.genes` (frozen for that run; gene definitions are
re-computed per future commit, but historical runs preserve their
detected set).

**Toggle-driven (Phase 2, forward direction):**

When we move to composable prompts, mutation = manifest of
`{gene: on/off}`. Compiler generates the prompts deterministically
from base + applied gene patches. Search API doesn't change —
`runs.genes` populated either by detection or by manifest keys
where value=on. Evolution orchestrator (§5c.609) becomes
combinatorial over toggles instead of LLM-as-merger.

Defer until Phase 1 produces enough mutation × gene data to know
which genes deserve to be promoted to first-class toggles.

**Catalogue maintenance.** `orchestra/genes.py` is a flat dict.
Adding a gene = one PR adding one entry + a marker in the prompt.
CI assertion: every gene name referenced in scenarios/yaml or
mutations table must exist in `GENES`. Removing a gene = needs
migration script (or just freeze: old runs keep the detected set
even if we drop the marker).

### 5e.13 Clients — web + CLI (human and agent-friendly)

Both are thin clients over `/api/*`. No backdoor access to DB or
FS — everything goes through HTTP so the same view works locally,
remote, or behind a reverse-proxy.

**Web UI** — additive routes in pr-analytics. Same surface as
described in 5e.6, gains pages for the new dimensions:

```
/qa/runs                    — list with filter chips (gene, mutation, project, …)
/qa/runs/{id}               — drill, including ↗ FS path + ↗ linked judge/agent
/qa/runs/{id}/files         — file-explorer over the FS trace tree
/qa/genes                   — gene catalogue + perf deltas
/qa/genes/{name}            — one gene's impact across mutations
/qa/mutations               — mutation list + lineage tree (parent_a / parent_b)
/qa/scenarios               — scenario catalogue
/qa/work_objects/{type}     — pivot view by file / pr / jira / project
/qa/regressions             — pick baseline + candidate, see deltas
```

**CLI — two output modes for two audiences.**

`bench-schedule` / `quality-cli` is a thin HTTP client. Same
commands, two output flavours:

```
quality-cli runs list                          # human: rich table, color
quality-cli runs list --json                   # agent: structured JSON, stable schema
quality-cli runs list --provider=qwen3-6 --gene=diff_view_block --since=24h

quality-cli runs get <id>                      # human: rich panel + step timeline
quality-cli runs get <id> --json               # agent: full run JSON, all events inline
quality-cli runs get <id> --files              # open the FS tree path

quality-cli tool-calls --tool=reflect --model=qwen3-6 --limit=5
quality-cli tool-calls --tool=reflect --model=qwen3-6 --limit=5 --json
                                               # agent: each row request+response paired

quality-cli search "PricingService.getCheapest" --in=findings --since=7d
quality-cli search "..." --json

quality-cli regressions --baseline=abc12 --candidate=def34
quality-cli aggregates by-gene --scope=tier:unit
quality-cli replay <run_id> --provider=qwen3-6   # re-run with same mutation, different provider

# Plan / queue (covered by §5c-5e earlier):
quality-cli plan create --branches=feature/X
quality-cli worker --provider=qwen3-6 --capacity=2
quality-cli watch <plan_id>                     # WS live progress
```

**`--json` mode contract** — what an LLM agent gets:

- Stable schema, documented in `/api/openapi.json`.
- Always `{"data": …, "meta": {...}}` envelope.
- Pagination cursors (not offsets) so the agent can iterate
  without race conditions.
- No interactive prompts, no color codes, no progress spinners.
- Errors return structured `{"error": {"code": ..., "message": ...}}`.
- `--quiet` suppresses everything except the JSON payload.

Default (`without --json`) is human mode: rich tables, panels,
colors, friendly error messages, may include suggestions like
*"try `quality-cli runs list --gene=…` to filter"*.

This makes the CLI **dual-purpose**: a developer types commands;
a debugging sub-agent (per §9) calls the same binary with `--json`
and reads structured output via standard `read_file` over its
stdout. No special "agent API" needed.

### 5e.10 Where it lives

- Server / DB / web UI: `pr-analytics/` (becomes the unified
  quality-api).
- Trace browser: moved from `diff-graph/tracing/` into
  `pr-analytics/routes/traces/`.
- Worker + CLI: `code-review-benchmarks/qa/` — `quality-cli`
  binary, only API client + worker loop.
- FS-only trace browser (secondary): standalone `bench-trace-fs/`
  app — same HTML/JS as the primary, swaps the storage adapter
  for `FilesystemTraceStore`. For ad-hoc dumps brought from
  remote machines, useful but not load-bearing.
- Diff-graph itself: untouched. The trace DB it writes to
  (`~/.diffgraph/traces.db`) is consumed by the server.

**Effort, recalibrated.**
- Phase 1 — schema additions + gene auto-detect + writer wiring:
  ~1 day. Pure refactor, no new server.
- Phase 2 — minimal SQLite-backed quality-api (read-only:
  `/api/runs`, `/api/runs/{id}`, `/api/tool_calls`, `/api/genes`):
  ~2-3 days. Boots pr-analytics as a FastAPI app for the first
  time.
- Phase 3 — write endpoints + dashboards + CLI client (human
  mode): ~1 week.
- Phase 4 — `--json` mode + agent-friendly invariants + minimal
  web UI for runs/genes/mutations: ~1 week.
- Phase 5 — workers + plans + lease/heartbeat: ~1 week.
- Phase 6 — live WS + regressions UI + FS-only trace browser:
  ~1 week.

Whole thing 4-5 weeks. Phases ship independently. Phase 1+2 is
the smallest useful chunk (better-than-grep over historical
traces) and is what I'm starting with.

---

## 5b. Jira context — agent reads the ticket and follows links

**Why.** Today the reviewer only sees the PR description. A real
reviewer reads the Jira ticket the PR claims to fix, follows links
("relates to", "blocks", "duplicates", "child of") to peer tickets,
and walks up to the epic to understand the broader effort. That
context turns a "cancelOrder NPE hotfix" diff from a single-line
review into "is this hotfix consistent with the wider null-safety
initiative the epic is tracking?"

**Sketch.**

- New domain tools the reviewer (and possibly investigator) can call:
  - `read_ticket(ticket_id)` — fetch summary, description, status,
    AC, current state, parent epic. Truncate huge bodies.
  - `list_ticket_links(ticket_id)` — return outgoing links with
    type and direction (`relates_to`, `blocks`, `is_blocked_by`,
    `parent`, `epic_link`, `duplicates`, …).
  - Optional: `walk_to_epic(ticket_id, max_depth=3)` — traverses
    parent / epic_link until it finds a ticket of type Epic; returns
    the path. Cheaper than asking the agent to compose two tools.
- Resolution:
  - Pull ticket id from PR title / branch name (e.g. `ORD-287` from
    `hotfix/ORD-287-cancel-npe`).
  - Make it a `_Ctx._jira_ticket_id` injected at run start; tools
    default to it when the agent calls them without args.
- Methodology nudge in reviewer.prompt LOOK phase:
  - "If the PR claims to address a Jira ticket, read it before
    forming concerns. If the ticket is part of a larger initiative
    (epic, sibling tickets), skim those too — a one-line hotfix
    inside a wider null-safety effort calls for different
    severity calibration than the same line in isolation."
- Provider abstraction: `diffgraph/jira.py` mirroring
  `bitbucket.py` — `fetch_ticket(id) → TicketContext` and
  `list_links(id) → list[Link]`. Auth + base URL via env vars
  (`JIRA_URL`, `JIRA_TOKEN`). Should be drop-in for any other
  tracker that exposes a similar shape.
- Bench scenarios: `setup.jira_tickets:` block — load fixtures
  from a YAML file mirroring Jira's REST shape so scenarios run
  hermetically without hitting a real Jira. Or stub via a mock
  provider in `bitbucket/base.py`-style.

**What we'd see in a passing run.**
- Agent reads `ORD-287` ticket, walks `is_part_of` link to
  `ORD-EPIC-NULL-SAFETY`.
- One of the findings cites the epic explicitly: "Epic
  EP-NULL-SAFETY mandates @PostLoad-driven invariants for all
  @OneToMany; this hotfix masks the symptom rather than meeting
  that direction."
- agent_warnings stays clean — no "methodology-gap: didn't
  consult ticket" because the agent did.

**Effort:** Medium. Tools + provider + prompt nudge + scenario
fixtures. Mostly well-trodden REST + closure pattern; biggest
work is fixture infra so bench can run without a live Jira.

---

## 6. Virtual Unified Diff Filesystem (ref-aware tools)

Goal: agent sees code through a virtual filesystem where files can be viewed at any commit or as unified diffs between commits. Three coordinate systems:
- **L** — virtual line number. Position in the unified diff view. Used for `start_line`/`end_line` in read_file, shown in outline, returned by search. The "working" coordinate for navigation.
- **old** — left-commit line number. Shown in read_file output columns. For referencing the old version.
- **new** — right-commit line number. Shown in read_file output columns. Used for Bitbucket comment anchoring and findings.

Each line has: `+` lines → L + new (no old), `-` lines → L + old (no new), context lines → L + old + new.

**Format (decided):** read_file shows two columns `old`/`new`. L is shown in outline and search results, used for read_file start/end. Outline shows `L` ranges for navigation + `old`/`new` ranges for reference. Search returns `L` + `old`/`new` per match.

**Backward compatibility:** when `ref="source"` (default, or `diff_mode: plain`), everything behaves exactly as current code:
- read_file shows plain file with line numbers (no old/new columns, no +/- markers)
- read_outline shows structure with line numbers and `*` on changed lines (current behavior)
- search returns file:line results (current behavior)
- L == line number == new (all the same, no virtual file concept)
- No prompt block injected
- `get_diff` tool remains available

The unified diff view (L, old/new, +/- markers, prompt block) only activates when `ref` is a range. Switching between modes = changing one yaml toggle. No code paths diverge at the tool level — `ref="source"` is just a degenerate case where VirtualFile has zero diff and L == new for all lines.

### Agent prompt block (injected when diff_mode=unified)

```
FILE VIEWING:
All files are shown as unified diffs between old and new versions.
Each line is marked: + (added), - (deleted), or blank (unchanged).

Line numbers:
- L (in outline and search results): position in the unified diff view.
  Use L for read_file(start_line, end_line) and to estimate read cost.
  L range includes both + and - lines, so it may be larger than the
  method appears in either version alone.
- old/new (in read_file output): line numbers in old/new commit versions.
  Use "new" line number for findings (Bitbucket comment anchoring).
  Blank "old" = line was added. Blank "new" = line was deleted.

Tools:
- read_file("Order.java", 20, 38)  → lines L20-L38 of the unified view
- read_outline("Order.java")       → methods with L ranges + old/new mapping
- search("getItems")               → matches with L number + old/new numbers
  Search covers both old (-) and new (+) content. You can find deleted code.

Example workflow:
  read_outline("Order.java")
  → [method] cancelOrder L20-38 (old:18-26 → new:18-28) *
  read_file("Order.java", 20, 38)   ← uses L range from outline
  → shows 19 lines: old context, deleted lines, added lines
  Finding: file="Order.java", line=25  ← uses "new" number for Bitbucket
```

Abstract ref names resolved at agent startup:
- `"source"` — tip of PR source branch / working tree (default, equivalent to current behavior)
- `"base"` — merge-base commit
- Single SHA (e.g. `"a1b2c3d"`) — specific commit
- Range with `..` (e.g. `"base..source"`, `"a1b..e4f"`) — unified diff mode

### 6.0 Store base_ref and source_ref in context

Pass merge-base SHA and source branch tip through `_Ctx` → tool closures.

- `bitbucket.py`: `fetch_pr` already computes merge-base, expose it in return value
- `cli.py`: for local diffs, compute base from git (e.g. `HEAD~1` or merge-base of branches)
- `_Ctx` gets `base_ref: str` and `source_ref: str` fields
- Tools resolve abstract names: `"source"` → `ctx.source_ref`, `"base"` → `ctx.base_ref`
- No agent-visible changes

**Where:** `diffgraph/orchestrator.py`, `diffgraph/bitbucket.py`, `cli.py`.
**Effort:** Small.

### 6.1 Add ref param to tools (hidden, default "source")

Add `ref` parameter internally to `read_file`, `read_outline`, `search` but do NOT expose in agent tool schema yet.

```
read_file(path, start_line?, end_line?, ref="source", line_numbers=true)
read_outline(path, ref="source")
search(query, ref="source", glob?)
```

When `ref="source"`: read from filesystem — current behavior, full equivalence.
When `ref` is a SHA: `git show <ref>:<path>` for read_file/outline, `git grep <ref>` for search.

Ref set centrally at agent creation time, not by agent.

**Test:** `ref="source"` produces identical results to current code.
**Where:** `diffgraph/orchestra_tools.py`.
**Effort:** Medium.

### 6.2 Virtual unified diff file generation

Core data structure. When `ref` contains `..` (range), generate virtual unified diff files.

Virtual file = right-commit file content with deleted lines (`-`) inserted at correct positions. Own line numbering (vL) that includes both `+` and `-` lines.

**Implementation:**
1. Parse unified diff hunks for the file (`git diff <left> <right> -- <path>`)
2. Walk right-commit file line by line
3. At hunk positions, insert `-` lines from the diff
4. Mark each line: `+` (added in right), `-` (deleted from left), ` ` (context/unchanged)
5. Track triple mapping:
   - `+` lines → vL + RC (no LC)
   - `-` lines → vL + LC (no RC)
   - context lines → vL + RC + LC
6. Mappings: `vl_to_rc`, `rc_to_vl`, `vl_to_lc`, `lc_to_vl`

```python
@dataclass
class VirtualLine:
    content: str                       # line text (without +/- prefix)
    marker: str                        # "+", "-", or " "
    L: int                             # virtual position (always present)
    old: int | None                    # left-commit line (None for `+` lines)
    new: int | None                    # right-commit line (None for `-` lines)

@dataclass
class VirtualFile:
    lines: list[VirtualLine]
    L_to_new: dict[int, int]           # L → new (absent for `-`)
    new_to_L: dict[int, int]           # new → L
    L_to_old: dict[int, int]           # L → old (absent for `+`)
    old_to_L: dict[int, int]           # old → L
```

**Special cases:**
- `ref="base..source"` → `git diff <base_ref> <source_ref> -- <path>`
- `ref="base"` shortcut for single ref (not a range) → read file at base_ref
- File only in right commit (new file) → all lines are `+`, vL == RC, no LC
- File only in left commit (deleted file) → all lines are `-`, vL == LC, no RC

**Test:** virtual file with zero diff = identical to real file, vL == RC for all lines.
**Where:** new `diffgraph/virtual_fs.py` or `orchestra/virtual_diff.py`.
**Effort:** Medium-Large.

### 6.3 read_file with ref range

When `ref` is a range, `read_file` displays virtual unified diff file with `old`/`new` columns:

```
# OrderService.java  ref=a1b..e4f  lines L20-38
  old  new
   18   18 |     public void cancelOrder(Long orderId) {
   19   19 |         Order order = orderRepository.findById(orderId)
   20      | -           .orElseThrow(RuntimeException::new);
        20 | +           .orElseThrow(() -> new OrderNotFoundException(orderId));
        21 | +       if (order.getItems() != null) {
   21   22 |             for (OrderItem item : order.getItems()) {
```

- `old` = line number in left (old) commit, blank for `+` lines
- `new` = line number in right (new) commit, blank for `-` lines. **Use for findings/Bitbucket comments.**
- `start_line`/`end_line` = L (virtual position, from outline). Header shows `lines L20-38`.
- `line_numbers=true` shows old + new columns, `line_numbers=false` hides them
- `+`/`-`/` ` markers always shown when ref is a range
- Header shows file, ref, and L range so agent can orient

**Where:** `diffgraph/orchestra_tools.py` `read_file_tool`.
**Effort:** Medium.

### 6.4 search over virtual unified diff files

When `ref` is a range, `search` operates on virtual unified diff files.

- Generate virtual files for all files changed in the range
- Search across virtual file contents (includes both `+` and `-` lines)
- Return results with L (virtual position) + `old`/`new` line numbers:
  ```
  OrderService.java L26 old:22      | -                 inventoryClient.release(item);
  OrderService.java L27      new:23 | +                 inventoryService.releaseInventory(item);
  PricingService.java L15 old:15 new:15 |       return order.getItems().stream()
  ```
- Agent uses L from search result to call `read_file(path, L-5, L+5, ref=...)` for context
- Agent uses `new` number from search result for findings
- Agent can find deleted code (in `-` lines) and added code (in `+` lines) in one search
- Unchanged files: search falls through to right-commit version (no virtual file, no overhead)

**Where:** `diffgraph/orchestra_tools.py` `search_tool`.
**Effort:** Medium.

### 6.5 read_outline with ref range

When `ref` is a range, outline shows method positions mapped to the virtual unified diff file.

**Implementation:**
1. Run outline on RIGHT commit → methods with RC line numbers
2. Run outline on LEFT commit → methods with old line numbers
3. Use `rc_to_vl` mapping to convert RC positions → vL positions
4. Mark `*` on methods whose body differs between left and right
5. Optionally show which commit(s) touched the method

**Output:**
```
# OrderService.java  ref=a1b..e4f
[method] cancelOrder   L20-38 (old:18-26 → new:18-28) *
[method] processOrder  L8-26  (old:8-26 → deleted) *
[method] validateOrder L28-37 (added → new:8-17) *
[method] executeOrder  L39-52 (added → new:19-32) *
[method] auditLog      L54-57 (added → new:34-37) *
[method] getOrder      L60-70 (old:80-90 → new:85-95)
```

- `L` = position range in unified diff view. **Use for read_file start/end.**
- `old:N-M` = line range in left (old) commit. Blank/`deleted` for added methods.
- `new:N-M` = line range in right (new) commit. Blank/`added` for deleted methods. **Use for findings.**
- `*` = changed in this range
- Agent calls `read_file("OrderService.java", 20, 38, ref="a1b..e4f")` using L range

**Complexity:** Highest of all phases. Depends on virtual file mapping from 6.2.
**Where:** `diffgraph/orchestra_tools.py` `read_outline_tool`.
**Effort:** Large.

### 6.6 Expose ref param to agent + yaml toggle

Make `ref` visible in tool schemas so agents can use it directly.

- Add `ref` to OpenAI tool schema for `read_file`, `read_outline`, `search`
- `config.yaml` toggle:
  ```yaml
  review:
    diff_mode: unified    # default ref="base..source" for all tools
    # diff_mode: plain    # default ref="source" (current behavior)
  ```
- When `diff_mode: unified`, agent sees diff markers by default without setting ref
- Agent can still override ref per-call (e.g. `ref="a1b..e4f"` for commit-by-commit)
- ~~Remove `get_diff` tool from agent prompts (replaced by `read_file` with ref range)~~ — done; tool unregistered, references purged
- ~~Update prompt instructions: explain ref, vL vs RC, when to use each~~ — done; reviewer/investigator system prompts now have a "Diff view" section with ref/L/old/new

**Where:** `diffgraph/orchestra_tools.py`, `config.yaml`, prompts.
**Effort:** Medium.

### 6.7 Commit list in agent prompt + commit-by-commit review

Pass PR commit list to agents for incremental review within one session.

- Fetch commit list from Bitbucket API (`/commits`) or `git log base..source`
- Add COMMITS section to agent prompt:
  ```
  COMMITS (oldest → newest):
    a1b2c3d  Add Promotion entity and repository
    e4f5g6h  Add PricingService bulk discount logic
    i7j8k9l  Add PromotionController endpoints
    m0n1o2p  Add tests
  ```
- Agent uses `ref="a1b..e4f"` to review specific commit ranges
- Lead decides strategy: whole diff vs commit-by-commit based on PR structure
- Update `lead.prompt` and `reviewer.prompt` with instructions

**Where:** `diffgraph/bitbucket.py`, `diffgraph/orchestrator.py`, prompts.
**Effort:** Medium.

### 6.8 Persistent PR review state (incremental across sessions)

Store review state per PR for incremental review across multiple runs.

- New SQLite table: `pr_state(pr_url, last_reviewed_commit, findings_json, context_summary, updated_at)`
- On run: detect if same PR was reviewed before → load previous state
- Agent prompt includes: `"Previously reviewed up to commit <SHA>. Previous findings: [...]"`
- Agent reviews only new commits (`ref="<last_reviewed>..source"`)
- Handle force-push/rebase: detect SHA mismatch → discard stale state, full review
- Handle amended commits: compare tree SHA, not commit SHA

**Future:** store state as Bitbucket PR comment (lives with the PR, visible to team).
**Where:** `orchestra/trace_db.py` or new `diffgraph/pr_state.py`.
**Effort:** Large.

### Rollout strategy

```
Phase 0-1: infrastructure, ref="source" = full equivalence, nothing breaks
Phase 2:   core — virtual unified diff with dual coordinates (vL + RC)
Phase 3-5: tools work with virtual FS, agent still doesn't see ref param
Phase 6:   yaml toggle diff_mode: unified — enable centrally, test end-to-end
Phase 7:   agent gets ref in schema + commit list — commit-by-commit review
Phase 8:   persistent state — incremental review across sessions
```

Each phase testable independently. Rollback at any stage: set `diff_mode: plain`.

---

## Priority Order

| # | Item | Impact | Effort | Priority |
|---|---|---|---|---|
| 1.1 | Budget context injection | High | Small | **Do first** |
| 3.1 | Budget balance prompt | High | Small | **Do first** |
| 3.2 | Reviewer efficiency prompt | Medium | Small | **Do first** |
| 3.3 | Diff filtering | Medium | Small | **Do first** |
| 5.1 | Total cost summary | Medium | Small | **Do first** |
| 6.0 | Store base_ref/source_ref in context | High | Small | **Do first** |
| 6.1 | Add ref param to tools (hidden) | High | Medium | **Do first** |
| 6.2 | Virtual unified diff file generation | **High** | Medium-Large | **Do second** |
| 6.3 | read_file with ref range (dual line numbers) | **High** | Medium | Do second |
| 6.4 | search over virtual unified diff files | High | Medium | Do second |
| 6.5 | read_outline with ref range | High | Large | Do second |
| 6.6 | Expose ref param + yaml toggle | High | Medium | Do third |
| 6.7 | Commit list + commit-by-commit review | Medium | Medium | Do third |
| 6.8 | Persistent PR review state | Medium | Large | Later |
| 1.4 | budget_status tool | High | Small | Do second |
| 1.3 | Pre-spawn validation | High | Medium | Do second |
| 2.1 | Agent prefix in trace | Medium | Small | Do second |
| 1.2 | Smart pushers | Medium | Medium | Do third |
| 1.6 | Wall-clock pusher + hierarchy propagation | **High** | Small | **Do first** |
| 5b  | Jira ticket reading + link traversal | **High** | Medium | **Do first** |
| 5c  | Pre-deploy QC pipeline (planner/scheduler/metrics/dashboard) | **High** | Large | **Do first** |
| 1.5 | Historical cost tracking | Medium | Medium | Do third |
| 2.2 | Live parallel progress | Medium | Medium | Do third |
| 4.1 | ~~Trace web server Phase 1 (basic server)~~ | ~~Done~~ | | |
| 4.1 | ~~Trace web server Phase 2 (live WebSocket)~~ | ~~Done~~ | | |
| 4.1 | ~~Trace web server Phase 3 (Alpine.js)~~ | ~~Done~~ | | |
| 4.1 | Trace web server Phase 4 (comparison + search) | Medium | Medium | Do third |
| 4.2 | Trace JSON export | Low | Small | Later |
| 4.3 | Trace search CLI | Low | Small | Later |
| 5.2 | Model comparison | Low | Medium | Later |
| 7.1 | `dg:` tag in comments (done) | **Done** | | |
| 8.2 | ~~pr-analytics `dg:` tag extraction~~ | **Done** | | |
| 8.3a | ~~benchmarks `--prompts` URI~~ | **Done** | | |
| 8.3b | ~~benchmarks capability tags~~ | **Done** | | |
| 8.4a | ~~webhook route management API~~ | **Done** | | |
| 8.1 | ~~Tracing subproject CLI~~ | **Done** | | |
| 8.3c | ~~benchmarks capability breakdown~~ | **Done** | | |
| 8.4b | ~~webhook POST/DELETE routes API~~ | **Done** (in 8.4a) | | |
| 7.5 | Evolution core: Branch, Population, tick() | **High** | Large | Do second |
| 7.6 | MutationAgent (capability-driven) | High | Medium | Do second |
| 7.7 | MergeAgent (semantic prompt merge) | High | Medium | Do third |
| 7.8 | Evolution dashboard + capability heatmap | High | Medium-Large | Do third |
| 7.9 | Evolution meta-agent (gardener) | Medium | Medium | Later |
| 7.10 | Cross-run memory per repo | Medium | Medium | Later |

---

## 7. Evolution — Self-Sustaining Agent Development

Subproject `evolution/` — population of long-running prompt branches competing continuously. Branches spawn children, accumulate fitness from benchmarks + business metrics, and converge when one dominates.

### 7.1 Architecture

```
                    ┌──────────────────┐
                    │   evolution.py    │
                    │   tick() loop     │
                    │   Population      │
                    │   Bandit          │
                    └────────┬─────────┘
                             │
        ┌────────┬───────┬───┴───┬────────┬────────┐
        ▼        ▼       ▼       ▼        ▼        ▼
   Tracing    Analytics Bench  Webhook  Mutation  Merge
   CLI        CLI       CLI    API      Agent     Agent
        │        │       │       │        │        │
        ▼        ▼       ▼       ▼        ▼        ▼
   traces.db  pr-ana   bench-  webhook  Orchestra Orchestra
              lytics   marks   .toml    agent     agent
```

Six connectors. Three are CLIs, one is API, two are Orchestra agents.

### 7.2 Connectors

**TracingConnector** — subproject `tracing/` with its own CLI:
```bash
tracing metrics --hash abc123 --since 2026-04-01
tracing compare --a abc123 --b def456
tracing runs --hash abc123 --format json
```
Returns: tokens_per_finding, convergence_steps, findings_avg, cache_ratio, tool_waste.

**AnalyticsConnector** — existing `pr-analytics` CLI:
```bash
pr-analytics acceptance --dg-hash abc123
pr-analytics compare --dg-hash-a abc123 --dg-hash-b def456
```
Returns: acceptance_rate, false_positive_rate, feedback_rate. Linked via `dg:` tag in comments.

**BenchmarkConnector** — existing `code-review-benchmarks` CLI:
```bash
benchmark run --prompts=bitbucket://...refs/mut-042/prompts
benchmark ab --a URI_A --b URI_B
```
Returns: overall_score, by_capability breakdown, by_scenario scores, regressions.

**WebhookConnector** — webhook router API:
```bash
curl POST /api/routes -d '{name, when, agent, prompts_uri, sample}'
curl PATCH /api/routes/mut-042 -d '{sample: 15}'
curl DELETE /api/routes/mut-042
```
Manages traffic allocation per branch.

**MutationAgent** — Orchestra agent that generates prompt mutations:
- Reads current prompt + traces + metrics
- Analyzes weaknesses (driven by benchmark capability scores)
- Proposes mutation with hypothesis
- Generates prompt diff, validates single-axis change

**MergeAgent** — Orchestra agent for semantic prompt merge:
- Git merge doesn't work for prompts (text conflicts = nonsense)
- Reads ancestor + branch A + branch B
- Understands each branch's improvement semantically
- Combines both coherently, resolves conflicts with reasoning
- Validates: no contradictions, improvements preserved

### 7.3 Entities and lifecycle

```python
class Branch:
    id: str                    # "mut-042-budget"
    parent_id: str | None      # "main" or "mut-031-tools"
    prompt_ref: str            # bitbucket://...refs/mut-042/prompts
    prompt_hash: str           # commit SHA
    axis: str                  # "budget", "security", "methodology"
    hypothesis: str            # "reviewer budget 15k→20k"
    status: Status             # BORN → BENCHMARKED → ACTIVE → DOMINANT → MERGED | EXTINCT
    sample_pct: float          # current traffic (0-100)
    generation: int            # distance from main (0, 1, 2...)

class Status:
    BORN          # created, no data yet
    BENCHMARKED   # passed benchmark gate
    ACTIVE        # receiving traffic, accumulating metrics
    DOMINANT      # consistently best, merge candidate
    MERGED        # became new main
    EXTINCT       # fitness too low, traffic removed
```

Lifecycle:
```
BORN ─── benchmark ───→ BENCHMARKED ─── deploy(5%) ───→ ACTIVE
  │       (fail)             │                            │
  ▼                          │                    measure() daily
EXTINCT                      │              ┌──────┼──────┐
                             │          fitness↑  breed()  fitness↓
                             │          sample↑    │       sample↓
                             │              │      │         │
                             │          DOMINANT  children  EXTINCT
                             │              │      BORN
                             │     converge (p<0.01, >14d)
                             │              │
                             │           MERGED (→ new main)
```

### 7.4 Fitness model

Benchmark is an equal signal to business metrics — fast, precise, tests deep capabilities.

```
fitness = 0.35 × benchmark_score        # deep capability (fast, precise)
        + 0.35 × acceptance_rate         # real-world impact (slow, noisy)
        + 0.20 × (1 / tokens_per_finding) # cost efficiency
        + 0.10 × feedback_rate            # developer engagement
```

Weights adjustable by evolution agent. Benchmark provides immediate signal; business metrics confirm over weeks.

### 7.5 Benchmark as capability map

Golden PRs test specific deep capabilities. Benchmark score breaks down by capability:

```
by_capability:
  business_logic:     0.85
  security:           0.70  ← weakest
  architecture:       0.60  ← weakest
  null_safety:        0.90
  transaction_safety: 0.75
```

**Capability-driven mutation:** MutationAgent sees "security=0.60" → proposes mutation targeting security awareness. New golden PRs expand the capability map — adding "ops_knowledge" scenario immediately reveals all branches score 0 there → stimulus for new mutations.

Golden PR suite evolves alongside agent:
- New capabilities (tools, sub-agents, knowledge bases) → new scenarios testing them
- Scenarios get harder as agent improves
- Historical bugs, architectural patterns, deployment risks — all testable

### 7.6 Metrics (three categories)

#### Business (pr-analytics, via `dg:` tag, slow signal)

| Metric | What it measures |
|---|---|
| acceptance_rate | Are findings accepted by developers? |
| false_positive_rate | How much noise? |
| feedback_rate | Are developers engaging? |
| time_to_merge_delta | Speed impact on PRs |

#### Quality (benchmarks, fast precise signal)

| Metric | What it measures |
|---|---|
| benchmark_score | Overall quality |
| by_capability.{X} | Per-capability depth |
| required_found | Coverage of known issues |
| regressions | What broke vs previous |

#### Efficiency (tracing CLI)

| Metric | What it measures |
|---|---|
| tokens_per_finding | Cost per result |
| convergence_steps | How fast agent settles |
| cache_ratio | Prompt caching efficiency |
| tool_waste_ratio | Redundant tool calls |

### 7.7 Core loop: `tick()`

Called daily by cron. Evolution agent can adjust parameters.

```python
def tick(self):
    measurements = self.measure_all()  # tracing + analytics + benchmark

    # 1. Rebalance traffic (Thompson sampling bandit)
    allocations = bandit(self.branches, measurements)
    for branch, pct in allocations.items():
        self.webhook.update_sample(branch.route_name, pct)

    # 2. Breed high-fitness branches
    for branch in self.top_branches(n=2):
        if branch.fitness > config.breed_threshold:
            # MutationAgent analyzes weaknesses, proposes child
            analysis = self.mutation.analyze(branch)
            # Benchmark capability scores drive mutation axis
            weakest = min(analysis.by_capability, key=lambda k: analysis.by_capability[k])
            child = self.mutation.propose(branch, axis=weakest)
            self.create_child(branch, child)

    # 3. Kill low-fitness branches
    for branch in self.active_branches():
        if branch.fitness < config.extinct_threshold:
            self.kill(branch)  # → EXTINCT, sample→0

    # 4. Detect convergence
    for branch in self.active_branches():
        if (branch.fitness > main.fitness
            and self.significant(branch, main, p=0.01)
            and self.dominant_days(branch) >= 14):
            branch.status = DOMINANT  # candidate for merge

    # 5. Merge dominant pairs (semantic merge)
    for a, b in self.dominant_pairs():
        merged = self.merge_agent.merge(ancestor="main", a=a, b=b)
        self.create_child(main, merged)  # new branch with both improvements
```

### 7.8 Evolution agent (meta-controller)

Does not manage mutations directly — adjusts automation knobs:

```python
# Tools available to evolution agent
evolution_status()              # population tree + measurements
evolution_set_config(           # adjust automation
    w_benchmark=0.4,            # "benchmark matters more now"
    breed_threshold=0.6,        # "breed more aggressively"
    max_branches=7,             # "allow more diversity"
)
evolution_spawn(                # manual spawn
    parent="main",
    axis="security",
    hypothesis="add OWASP top-10 checklist to reviewer prompt"
)
evolution_approve_merge(        # human-in-the-loop
    branch_id="mut-042"
)
```

Sees: population tree with fitness, capability heatmap, metric trends.
Decides: weight adjustments, strategic spawns, merge approvals.
`tick()` runs automatically. Agent is the gardener, not the engine.

### 7.9 Safety guardrails

| Guard | Trigger | Action |
|---|---|---|
| Benchmark regression | any capability score drops >10% | Block deployment |
| Rate limit | mutation > N comments on PR | Reduce sample, alert |
| Fitness collapse | acceptance_rate < 50% for 7 days | Auto-kill branch |
| Population cap | > max_branches active | Cull lowest fitness |
| Main protection | main always ≥ 30% traffic | Bandit constraint |
| Merge approval | DOMINANT → MERGED | Requires human/agent approval |

### 7.10 Cross-run memory

Per-repo learned patterns injected into prompts:
- "This codebase uses @Builder.Default — null checks are false positives"
- "Team prefers explicit error handling over @SneakyThrows"
- Aggregated from trace DB + pr-analytics acceptance by repo.
- New `{learned_patterns}` placeholder in prompts, updated weekly.

### 7.11 Population visualization

```
main (gen-0) ─────────────────────────────────── 40%  fitness=0.72
  ├── mut-042-budget ──────────────────────────── 20%  fitness=0.78 ↑ ACTIVE
  │     ├── mut-042a-budget+tools ─────────────── 8%  fitness=0.81 ↑ ACTIVE
  │     └── mut-042b-budget+severity ──────────── 5%  fitness=0.74   ACTIVE
  ├── mut-051-security ────────────────────────── 15%  fitness=0.76 ↑ ACTIVE
  └── mut-053-methodology ─────────────────────── 0%  fitness=0.65 ↓ EXTINCT
                                                  └── killed: fitness below threshold

Capability heatmap:
              main  mut-042  mut-042a  mut-051
business_logic 0.85   0.85     0.87     0.83
security       0.60   0.62     0.65     0.78  ← mut-051 wins here
architecture   0.70   0.72     0.75     0.68
null_safety    0.90   0.92     0.93     0.88
```

Evolution is continuous. Branches compete for weeks. Best spawn children. Weakest die. Dominant branches merge. System improves generation by generation, driven by benchmark capabilities + business metrics + efficiency.

---

## 8. External System Gaps (prerequisites for evolution)

Audit of what each connected system needs before evolution connectors work.

### 8.1 Tracing — needs CLI subproject

**Exists:** SQLite DB (runs + events), JSON API, WebSocket live, prompt_source/prompt_hash columns.

**Missing:**

| Gap | What to build | Effort |
|---|---|---|
| No CLI | `tracing/` subproject with typer CLI | Medium |
| No metrics aggregation | `tracing metrics --hash X` → tokens_per_finding, convergence_steps, findings_avg, cache_ratio, tool_waste | Medium |
| No compare | `tracing compare --a X --b Y` → delta per metric + p-value + runs count | Medium |
| No API endpoint | `GET /api/runs/metrics?hash=X` on trace server | Small |
| No run tagging | Tag runs for filtering (experiment, stable, benchmark) | Small |

**Priority:** High — evolution tick() calls tracing on every cycle.

### 8.2 pr-analytics — needs `dg:` tag extraction

**Exists:** Comment storage, reactions, LLM-judge verdicts, semantic_acceptance_rate, sql command.

**Missing:**

| Gap | What to build | Effort |
|---|---|---|
| No `dg:` tag parsing | `extract_dg_tag(text) → {gen, hash, run}` regex parser | Small |
| No tag in DB | Extract and store `dg_gen`, `dg_hash`, `dg_run` on comment cache | Small |
| No acceptance by hash | `acceptance --dg-hash X` command → acceptance_rate, false_positive_rate, feedback_rate | Small |
| No compare by hash | `compare --dg-hash-a X --dg-hash-b Y` → delta + significance | Medium |
| No trend by hash | `trend --dg-hashes X,Y,Z` → acceptance over time per generation | Medium |

**Priority:** Critical — this is the bridge between trace DB and business outcomes. Without it, fitness function has no business signal per prompt_hash.

### 8.3 code-review-benchmarks — needs `--prompts` + capability breakdown

**Exists:** Scenario runner, LLM judge, per-scenario scoring, A/B comparison, results storage.

**Missing:**

| Gap | What to build | Effort |
|---|---|---|
| No `--prompts` URI | Add `--prompts` arg to `run` command → pass to agent CLI trigger | Small |
| No capability tags | Add `capabilities: [security, business_logic]` to scenario YAML | Small |
| No capability breakdown | Aggregate scores by capability tag across scenarios | Medium |
| No weaknesses API | `weaknesses --run X` → lowest scoring capabilities | Small |
| No per-capability regression | Compare capability scores between runs, not just overall | Medium |

Scenario YAML change:
```yaml
metadata:
  difficulty: medium
  language: java
  capabilities: [business_logic, null_safety, transaction_safety]  # NEW
```

**Priority:** High — benchmark capability scores drive MutationAgent's axis selection.

### 8.4 Webhook router — needs route management API

**Exists:** `POST /webhook`, `GET /routes` (read-only), `GET /health`, TOML config.

**Missing:**

| Gap | What to build | Effort |
|---|---|---|
| No create route | `POST /api/routes` → add route + agent in memory + persist | Medium |
| No update route | `PATCH /api/routes/{name}` → update sample%, agent | Small |
| No delete route | `DELETE /api/routes/{name}` → remove route | Small |
| No hot reload | Apply changes without restart (file watcher or `/api/reload`) | Medium |
| No agent management | `POST /api/agents` → register new agent config | Small |
| No validation | `POST /api/validate-route` → test when expression | Small |

**Priority:** High — evolution deploy/undeploy/rebalance all need programmatic route management.

### Implementation order

```
Phase 1 (unblock evolution core):
  8.2  pr-analytics dg: tag extraction        ← Critical, small
  8.3a benchmarks --prompts URI               ← Small
  8.3b benchmarks capability tags in YAML     ← Small
  8.4a webhook PATCH /api/routes/{name}       ← Small (sample% update)

Phase 2 (full connectors):
  8.1  tracing CLI + metrics/compare          ← Medium
  8.3c benchmarks capability breakdown API    ← Medium
  8.4b webhook POST/DELETE /api/routes        ← Medium
  8.2b pr-analytics compare/trend by hash     ← Medium

Phase 3 (polish): ✅ Done
  8.4c webhook hot reload                     ✅
  8.1b tracing run tagging                    ✅
  8.3d benchmarks per-capability regression   ✅
```


## 9. Workspace as files — cross-source context for agents

**Core idea.** Today the agent only sees the PR repo. We want it to
also see related git repos (DB migrations, k8s manifests, IaC,
architecture-as-code) AND non-repo sources (Jira, Confluence,
Teable architecture, past code reviews, prod incident postmortems).
Instead of inventing tools per source — i.e. a generic raw-HTTP
tool, or 14 narrow per-source tools — we **materialise everything
that is fundamentally a document into the same workspace**, mounted
as plain files alongside the PR repo. The agent uses the existing
`list_files` / `read_file` / `search` / `read_outline` to inspect
all of it. **Cross-source `search()` is the killer feature** — one
grep finds a string in Java code, in DDL, in a Jira description,
and in an ADR all at once.

The boundary: things that are inherently **documents** (issues,
manifests, postmortems, ADRs, past reviews) → dumped as files.
Things that are inherently **queries** (live time-series metrics,
active alerts) → keep behind a small API tool. Rule of thumb:
"is this source a set of documents, or a stream of measurements?"

The agent gets **zero new tools** for the document half. The number
of tools stays where it is.

### 9.1 Workspace layout

```
workspace/
├── orderflow/                     ← PR repo (base..source diff view)
├── orderflow-migrations/          ← sibling repo, plain (ref=source, no markers)
├── orderflow-k8s/                 ← sibling repo, plain
├── jira/
│   ├── ORD-234.md                 ← issue dump as markdown
│   ├── ORD-291.md
│   ├── EPIC-100.md
│   └── BUG-512.md
├── architecture/
│   ├── orderflow-c4-context.md    ← Confluence page dump
│   ├── orderflow-c4-container.md
│   └── ADR-0023-promotions.md
├── teable/
│   └── service-catalog.md
└── reviews/
    └── PR-742.md                  ← past review dump (findings + verdict + linked tickets)
```

Path prefix tells the agent which kind of source it's looking at;
DiffSearch VFS already supports plain mounts (ref=source, no
markers) for siblings — only the primary repo is in unified-diff
mode.

### 9.2 AGENTS.md as the workspace manifest

`AGENTS.md` already lives in the PR repo and is the canonical
"things the agent should know about this project" doc. We extend
it with declarative `## Related <source>` sections that name what
to mount and give a free-text trigger hint the LLM uses to decide
when to actually look:

```markdown
## Related repositories

Cross-repo dependencies — clone these into the workspace as
siblings:

- **migrations** — `ssh://git@bitbucket-ci/SBLOOM/orderflow-migrations`
  Check when this PR touches JPA entities, `@Column` annotations,
  repository methods, or anything that implies a schema shape.

- **k8s** — `ssh://git@bitbucket-ci/SBLOOM/orderflow-k8s`
  Deployment specs. Check when env vars, config keys, service URLs
  change.

## Related Jira

- **ORD project** — `https://jira/.../ORD`
  Auto-fetch tickets referenced from the PR description, plus their
  parent epics, plus any linked `BUG-*` tickets so prod-incident
  history is visible.

## Related architecture

- **C4** — `https://confluence/.../ARCH/Orderflow` (depth=2)
  Fetch when this PR adds/removes a service boundary or changes a
  contract between services.

## Related past reviews

- Auto-fetch reviews from last 6 months for files this PR touches.
  Use to detect "this BLOCKER was raised before — has it
  regressed?" and to learn the project's historical concerns.
```

Decentralised: each repo owns its own AGENTS.md. PR on
orderflow-migrations has its own AGENTS.md where orderflow is
listed as a sibling. Symmetry by design.

Markdown structure is **fixed enough to parse** (regex for header
+ bullets), **free enough for LLM to read directly**. Each bullet:
`- **<name>** — \`<url>\`` followed by free-text trigger hint.

### 9.3 Document/query boundary per source

| Source | Nature | Approach |
|---|---|---|
| code, DB migrations, k8s, IaC | git repos | shallow clone → mount sibling at `ref=source` |
| Jira issues / Confluence / Teable / ADRs | documents | fetch → render to `.md` with YAML frontmatter |
| past code reviews | structured records (we have SQLite DB) | render to `reviews/PR-N.md` |
| prod incident postmortems | documents (after-the-fact) | fetch → `.md`, just like Jira |
| live prod metrics (Grafana, Prometheus) | time-series queries | API tool `metrics_query(service, metric, since)` |
| live alerts / on-call status | live state | API tool `incidents_active(...)` |

For the document half: zero new tools. For the query half: one or
two narrow tools, scope deferred until the document approach
proves out.

### 9.4 Selectivity — what NOT to dump

You can clone a whole git repo cheaply. You **cannot** dump a
whole Jira project (could be 100k tickets). Each non-repo source
needs a selector that produces a small relevant set:

- **Jira:** parse PR description for keys (`ORD-234`, `BUG-512`),
  fetch those + their `linked` and `parent` (1 hop). Add `recent`
  filter from AGENTS.md (`recent=90d`) for always-relevant tickets
  (e.g. open epics this team is tracking).
- **Confluence:** AGENTS.md lists a root page + depth (`depth=2`).
  Fetch that page tree.
- **Teable:** AGENTS.md lists table id + filter; dump matching rows
  as one markdown table per query.
- **Past reviews:** filter by file paths the PR touches (last 6
  months by default). We already have these in our SQLite trace DB.
- **Postmortems:** linked from `linked: [BUG-*]` of fetched Jira
  tickets — recursive depth=1.

All selectors merge their key sets, dedupe, batch-fetch, render to
files.

### 9.5 Markdown rendering format

Frontmatter for structured metadata (frontend grep / agent
filter), body for prose (semantic search):

```markdown
---
key: ORD-234
type: Story
status: In Progress
epic: ORD-100
linked: [BUG-512, BUG-489, ORD-180]
created: 2026-04-12
updated: 2026-05-08
labels: [pricing, promotions]
---
# ORD-234: Implement buy-3-get-1-free promotions

## Description
…

## Acceptance criteria
…

## Recent activity
- 2026-05-08 [Andrey] commented: "the cheapest-item logic in
  PricingService needs review per AGENTS.md rule"
- 2026-05-05 [Bob] linked BUG-489: "earlier prod issue, similar
  shape"
```

Same shape for Confluence, ADRs, postmortems, past reviews —
metadata up top, prose below. `linked: [...]` lists are how the
agent walks the cross-source graph: read `BUG-512.md`, see it
references `ORD-234`, open that, etc. — same UX as
`read_thread(parent_id)`.

### 9.6 Closes-the-loop value

Two things this directly enables:

- **"What past bugs hit this code?"** — `search("PricingService")`
  finds Java + Jira `BUG-*.md` simultaneously. Reviewer sees "this
  exact class had a prod NPE 3 months ago" without any new
  primitive. Frontmatter `linked: [postmortem-X]` continues the
  trail.
- **"What was raised in prior reviews of this file?"** —
  `reviews/PR-N.md` files in workspace. Reviewer's LOOK phase can
  optionally `list_files("reviews/")` and notice "BLOCKER about
  cheapest-item was already raised once and resolved — has the
  current diff regressed it?" Critical for projects with high
  iteration speed.

Both are the user's stated goals ("на что обращать внимание + какие
проблемы бывают на проде") with **zero new tools** — pure
materialisation + grep.

### 9.7 Implementation phasing

Big move, ship in slices. Each phase delivers value
independently — don't wait for the full vision to land.

**Phase 9-A: sibling repos (migrations + k8s).**
~150-200 LOC + 1-2 DiffSearch tests + a fixture toy migrations
repo for the bench. Smallest, highest-value first slice. Reviewer
immediately sees DDL alongside JPA changes.
- AGENTS.md `## Related repositories` parser.
- Parallel shallow-clone after primary clone (lazy_init pipeline).
- DiffSearch VFS extended to mount siblings as plain.
- One bench scenario: PR on orderflow changes a `@Column`
  mapping, sibling migrations repo has the matching `ALTER TABLE`
  — reviewer should cross-reference.

**Phase 9-B: past reviews materializer.**
We already have all this data in `~/.diffgraph/traces.db`. Render
on workspace setup. ~100 LOC.
- `reviews/PR-N.md` with frontmatter `findings:`, `verdict:`,
  `prompt_generation:`, `linked_jira:`.
- Filter to files touched by current PR.
- Reviewer's LOOK phase gets a hint to glance at `reviews/`.

**Phase 9-C: Jira materializer.**
Highest-value external integration but needs Jira API client +
auth. Selector: PR description scan + 1-hop linked + parent epic.
~300 LOC including the Jira client.
- `## Related Jira` AGENTS.md section.
- Markdown frontmatter format from §9.5.
- One bench scenario: PR description references `ORD-234`, the
  fetched ticket links `BUG-512`, reviewer cites both in evidence.

**Phase 9-D: Confluence/Teable materializers.**
Same shape, different fetcher. ~200 LOC each. Add only when
Phase 9-C is paying off and a real use-case appears.

**Phase 9-E: live API tools (metrics + active incidents).**
The query half. Probably `metrics_query(service, metric, since)`
and `incidents_active(service)`. Defer until 9-A to 9-D are
exercised — by then we'll know what queries the agent actually
wants vs. what we imagined.

### 9.8 Open design questions

- **Lazy vs. eager mount.** Sibling repos are cheap (shallow
  clone) — eager. Jira/Confluence — also eager since selector
  produces a bounded set. Per-fetch lazy could come later if
  workspace gets too big.
- **Snapshot semantics across sources.** Materialise once at run
  start, freeze. Same as our `comment_tools` `max_id` snapshot.
  Updates mid-run not visible to current agent.
- **Auth.** Each source has its own creds (Bitbucket SSH key, Jira
  token, Confluence token). Plumb as env / per-source config block.
  Don't mix into AGENTS.md (that's per-project domain knowledge,
  not per-deployment ops).
- **PR description Jira-key extraction.** Need a regex that
  captures `[A-Z]+-\d+` patterns; deal with false positives
  (`HTTP-404`, `JIRA-XXXX` placeholders) via a project-prefix
  whitelist driven by AGENTS.md.
- **Render-to-markdown fidelity.** Confluence has tables, macros,
  inline images. Probably render to plain markdown with a
  `[image: …]` placeholder, ignore macros. Lossy but readable;
  always preserve a `source_url:` in frontmatter for the human if
  more depth is needed.
- **What if AGENTS.md is missing.** No related sources, current
  behaviour. No regression — opt-in by design.
- **What if a source is unreachable.** Graceful skip + warning
  recorded in trace; do not fail the run. One broken Confluence
  shouldn't take down a code review.
- **Symmetry.** A PR on orderflow-migrations has its OWN
  AGENTS.md where orderflow is listed as the sibling. The
  workspace builder follows the same recipe; it doesn't hard-code
  "primary = orderflow".

**Where:** new module `diffgraph/workspace/` — submodules per
materialiser (`repos.py`, `jira.py`, `past_reviews.py`,
`confluence.py`, …) + `manifest.py` for the AGENTS.md parser.
`diffgraph/orchestrator.py` calls a single
`build_workspace(pr_meta) → workspace_dir` after primary clone.
**Effort:** Phase 9-A small (1 day), 9-B small (½ day), 9-C
medium (2-3 days), 9-D each medium, 9-E small per tool.
