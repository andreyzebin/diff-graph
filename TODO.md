# Orchestra — Planned Improvements

> Quality-management architecture overview: [docs/qa-architecture.md](docs/qa-architecture.md).
> That doc is the single place describing how the unit / integration / production
> loops fit together, what merge_acceptance_rate is, and how select-golden bridges
> prod data into bench scenarios. This file (TODO.md) lists open work items;
> the qa-architecture doc describes the steady state.

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
- ~~Stage A: qa_tasks.resources dual-write~~ — URIs (`scenario://`, `lineage://`, `mutation://`, …) alongside legacy columns, 1222 historic rows backfilled
- ~~Stage B: drop legacy qa_tasks columns~~ — scenario_id / lineage / mutation_hash gone, queue renamed from provider, all readers use `json_each(resources)`
- ~~Stage 4: LLMJudge wiring for unit tier~~ — `bench run-unit` invokes the judge after the agent subprocess via FakeBenchPRView + UnitFixture→Scenario adapter; SQLite `runs` row with `kind='judge'` lights up /qa/scoring for unit fixtures
- ~~§5d.3 Phase A: per-agent unit fixtures with expected_output~~ — REV-U-001/002/003, INV-U-001/002, DISP-U-001/002 (all in `code-review-benchmarks/benchmark/scenarios/unit/`)
- ~~§5d.3 Phase B: unit-shape mirrors of legacy tier:unit scenarios~~ — REV-001 / INV-001 / DISP-001/002 / REV-002 mirrored under scenarios/unit/* on fake bitbucket
- ~~Leak detection~~ — tests/test_prompts_no_fixture_leak.py + benchmark/tests/test_unit_fixture_leak_check.py auto-derive forbidden keyword lists from fixtures; caught 7 real leaks in the May-2026 cleanup
- ~~Trend chart on /qa/scoring uses equal-spaced ordinal mutations~~ — was temporal, score "waves" got smeared by attempt-count density

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

### ~~3.4 Diff-view mental model — fixed via `changes_only` + fetch fallback~~

**Root cause** wasn't the prompt's "annotated" wording. It was a
combination of two tool-side bugs that left the agent staring at
output with no diff signal, after which `diff_search(^+)` and
`^M ` greps were a rational fallback ("I see files but no change
indicators — let me grep the content for diff markers"):

1. `diff_list_files` capped at 50 rows, sorted alphabetically, with
   no `changes_only` filter. On large repos the M/A/D rows fell past
   the cap; the agent saw 50 blank-marker rows and assumed nothing
   had changed.
2. `--filter=blob:none` fetch against Bitbucket Server silently
   returned incomplete trees, so `git diff --name-status BASE
   SOURCE` produced zero output and `materialize_vfs` recorded
   every file as unchanged.

Both fixed in this commit:

- `diff_list_files` gains `changes_only` (default `true`), `start`,
  and `n` pagination. Agents now get only the meaningful M/A/D rows
  by default and a pagination footer (`[showing X..Y of N — call
  with start=Z to see the next page]`) when more exist. Verified on
  the SBLOOM-142 run: `diff_list_files(changes_only=True)` returned
  exactly 2 changed files; the reviewer went straight to
  `diff_read_file` on each — zero `^+` grep fallbacks.
- `GitClient.fetch` retries without `--filter=blob:none` when a
  probe (`git ls-tree --name-only REF`) shows the trees didn't
  actually land — Bitbucket Server's partial-clone rejection
  surfaces instead of silently corrupting the diff.
- `materialize_vfs` logs a WARNING when `git diff` returns empty
  but the source tree is non-empty, so future regressions don't
  masquerade as "this PR has no changes".

Coverage added in `tests/test_diff_tool_schemas.py`
(`TestDiffListFilesChangesOnly` + `TestDiffListFilesPagination`)
and `diffsearch/tests/test_list_outline.py` (`TestChangesOnly`).

### ~~3.5 `react_to_comment` — removed entirely~~

Bitbucket Server has no public reactions REST endpoint (404 on
`/rest/api/1.0/.../comments/<id>/reactions/<emoticon>` across the
SBLOOM instance; the feature is UI-only on Data Center 8.x+). The
tool was first kept with a graceful-404 path, then removed
outright — the whole stack is gone: the `react_to_comment` orchestra
tool, `bitbucket.react_to_pr_comment` + `ReactionsUnsupportedError`,
the fake/api provider methods, the `tools` / `tools_add` entries in
the dispatcher + reviewer prompts, and `tests/test_react_unsupported.py`.
Agents acknowledge / follow up on existing threads with
`pr_post_comment(parent_id=...)` — a thread reply is a richer signal
than an emoji anyway. If reactions are ever needed on Bitbucket
Cloud (which does expose the API), re-add as a prompt-layer
`tools_add` extension for that environment specifically.

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
   `done(findings)` and `agent_spawn.focus`.
4. **Expected output** is a list of keyword groups (existing
   `concern_focuses`/`description_keywords` infra), AND-of-OR match
   against title + description fields of each reflect entry.

Coverage matrix today:

| Agent        | Has reflect? | Status |
|--------------|--------------|--------|
| reviewer     | yes          | REV-001-concerns ✅ landed (integration). Unit-tier fixtures: REV-U-001-store-credit-concerns + REV-U-002-cancel-npe-concerns ✅ — expected_output authored, judge wiring pending (Stage 4 below). |
| investigator | yes          | INV-U-001-cancel-npe ✅ — expected_output authored, asserts via `intended_findings` (done().findings) for the focused null-items investigation. INV-002 (integration) still todo — same shape: focus + AGENTS.md citations + questions_remaining via `reflect(learned, questions_remaining)`. |
| dispatcher   | no           | Add `reflect` to dispatcher's @tools if we want symmetric isolation, OR skip — dispatcher is a router with fewer "thinking" phases. Decide later. Unit fixture not yet authored — depends on the call: either we add reflect, or we assert directly on `agent_spawn.agent_name` + `pr_post_comment.text` from invocations.json (needs a new ExpectedOutput channel — `intended_routing` or similar). |

**Stage 4 — judge wiring landed.** `bench run-unit` now invokes
LLMJudge after the agent subprocess finishes when (a) the fixture
declares `expected_output`, (b) `~/.benchmark/config.yaml` has a
`judge:` block, and (c) an attempt_dir was provided (auto-derived
from BENCHMARK_TRACE_DIR for plan-fired tasks).

Pieces:

- `benchmark/runner/fake_view.py` — `FakeBenchPRView(AgentPRView)`
  reads back the agent's posted comments from the in-memory sink and
  serves them to the judge through the same interface
  RealBitbucketPRProxy uses. `get_diff` / `get_raw_file` shell out
  to `git diff` / `git show` against the temp clone the agent
  reviewed.
- `_build_scenario_from_unit_fixture` (in `run_unit.py`) — synthesizes
  a `Scenario` dataclass from a unit yaml's `expected_output` + tags +
  agent_data, so LLMJudge needs zero changes to consume the new
  yaml shape.
- `run_unit_fixture` accepts `attempt_dir` + `judge_cfg`; when both
  set, calls the judge under `attempt_dir/runs/judge/`. OTel
  covered on both sides — agent via `DIFFGRAPH_TRACE_PATH=agent_dir`,
  judge via `JudgeTraceWriter`. Each writes a `runs` row to
  `~/.diffgraph/traces.db` with `scenario_id`, `scenario_tags`,
  and `linked_run_id`.

Surfaces unlocked on the UI side:

- `/qa/scoring` lists the fixture once its first judge row lands
  (via `scenario_id_scored` dimension reading from `runs.kind='judge'`).
- `/qa/sessions/<run_id>` shows the agent's trace tree with its
  invocations + the judge's response.
- `/qa/plans` shows each task with its judge score after the bench
  subprocess returns.

Open follow-ups (not blocking unit-tier usage):

- Auto-fire smoke after every commit (§5e.16 tier:smoke) — schedule
  is missing.
- New `assert_via:` channels (`intended_reply`, `intended_routing`,
  `intended_tool_call`) — needed for dispatcher unit fixtures that
  don't use reflect.
- Reflect-quality methodology axis (separate channel — see
  "Reflect quality" section below).

#### Answer channels — reflect is not the only one

The reflect-only pattern works for reviewer concerns-only because
reflect is where concerns naturally live. But the principle is
broader: we hand the agent an input and ask it to surface its answer
through *some* channel we can read from invocations.json. Reflect is
one channel; there are others, each with its own ExpectedOutput
`assert_via:` value:

| Channel              | What the judge reads                       | Used by |
|----------------------|--------------------------------------------|---------|
| `intended_concerns`  | `reflect(questions_remaining=...)` titles + `agent_spawn(focus=...)` | reviewer concerns-only |
| `intended_findings`  | `done(findings=[...])` items                | investigator standalone (REV-U / INV-U / SCEN-009 isolation) |
| `pr_comments`        | real `pr_post_comment`/`post_general` posts via the fake PR sink | integration tier |
| `intended_reply` — TODO | `done(text=...)` plain-text or first-final-message text | dispatcher /ask /help, future text-mode agents |
| `intended_routing` — TODO | `agent_spawn(agent_name=...)` alone (ignore focus) | dispatcher routing-only |
| `intended_tool_call` — TODO | args of a test-only tool we hand the agent (e.g. `submit_answer(...)`) | agents that don't naturally end in done/reflect |

The last three need new channel constants in scenario_loader's
`assert_via` whitelist + matching extractors in judge.py
(`_load_intended_reply` / `_load_intended_routing` /
`_load_intended_tool_call`). All are mechanical extensions of the
existing `_load_intended_findings`/`_load_intended_concerns` pattern.

For agents whose default tool surface doesn't include a clean answer
sink, we can register a test-only tool at the registry level —
`submit_answer(text: str)` or similar — and have the user-message
override instruct the agent to call it. This keeps the system prompt
and methodology untouched while giving the unit test a deterministic
place to read the output from. No new orchestration code needed; just
a tool registration in the unit runner.

#### Reflect quality — a separate methodology axis, not the answer channel

When `reflect` is *also* the answer channel (reviewer concerns-only)
we score against it directly. But when the answer lives elsewhere
(done / submit_answer / plain text), the reflect calls become a
separate quality signal: do they happen at the right moments? are
they coherent? do they show the agent updating its mental model?

This belongs on the methodology axis (§5e.16 axis #2). Concrete
sub-signals:

- `reflect_called_at_least_once` — for agents whose @tools includes
  reflect, at least one call before done() suggests the agent
  actually paused to think. Zero reflects on a non-trivial scenario
  is a methodology red flag.
- `reflect_coherence` — successive reflects build on each other
  (`learned[t+1] ⊇ learned[t]` ish, `questions_remaining[t+1]`
  is a refinement of `[t]`). Reflects that drop facts or invent
  new unrelated questions = the agent isn't tracking state.
- `reflect_convergence` — `|questions_remaining|` trends DOWN over
  the reflect sequence as the agent answers / rules out / discards
  hypotheses. A flat or growing list = the agent is opening more
  questions than it closes, i.e. drifting outward instead of
  homing in. Acceptable for the first 1-2 reflects (still scoping)
  but not for the last 1-2 before done().
- `reflect_no_loops` — once a question is closed (removed from
  `questions_remaining` or marked resolved in `learned`), it must
  not reappear in a later reflect. Reopening already-closed
  questions = the agent isn't keeping memory of what it just
  figured out, which is the failure mode that burns the budget
  to zero without ever reaching a verdict.
- `reflect_confidence_calibration` — when reflect carries
  `confidence`, it should converge UPWARD (`low → medium → high`)
  as evidence accumulates, not random-walk and not claim "high"
  on the first reflect (overclaiming). Allow at most one drop
  (when the agent legitimately discovers contradicting evidence
  and revises); more drops = the agent is guessing.
- `reflect_rejection_visible` — when the agent rules a hypothesis
  out, the reasoning must surface in `learned` ("X is not the
  cause because Y") rather than the question silently disappearing.
  Silent drops are indistinguishable from "agent forgot the
  question" — and we want to be able to distinguish.

These are *judged separately from* the hard-skill answer. An agent
can have a great answer with bad reflects (lucky guess) or a bad
answer with good reflects (was on the right path, ran out of
budget). The unit-tier judge surfaces both axes so we can tell which
side regressed when scores move.

Implementation: a separate ExpectedOutput field
`expected_reflect_quality` (or a dedicated `assert_via:
[reflect_quality]` channel that triggers the methodology judge), with
optional knobs `min_calls` / `monotonic_confidence` / `coherence`.
Defer until we have a few scenarios where reflect-vs-answer mismatch
is the actually-observed failure mode worth catching.

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
prompt frames `agent_spawn` as a capability ("delegate depth to
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

> "When you have a concern, call `agent_list()` first to see who
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

`agent_list()` is already implemented — works with both real and
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

### 5d.4 Judge asserts — migrate `must_mention` word arrays to semantic `rationale`

The reply judge (`benchmark/runner/judge.py::REPLY_PROMPT`) documents
`must_mention` rows as **"matches if any of its alternatives appears
semantically (synonyms ok)"** — but in practice the word-array shape
nudges the LLM judge into surface keyword-matching even when its
docstring says otherwise. Two observed false-fails (plan 219 run
53b3ee6ee582 on DISP-U-001 — fixed by removing `must_address`; plan
221 run cd9da88f0166 on DISP-U-002 — fixed by removing `must_mention`)
both had ideal agent output that scored 0.0 because a row was marked
`matched=false`. The fix on the scenario side was to lift the contract
into prose `rationale` ("agent should greet back because the user
said hi") and rely on `forbidden_topics` + `acknowledgement_required`
as hard rails.

Action: audit every `must_mention` / `must_address` block in
`benchmark/scenarios/`:

- **Ack-style fixtures** (one-line greet-back / brief ack +
  delegate — DISP-U-001/002, SCEN-200/201/202/203 etc): drop the
  word-array fields entirely; move the contract into `rationale`
  with explicit "REQUIRED (semantic)" / "FORBIDDEN (semantic)"
  sections so the LLM judges by intent, not keyword presence.
- **Substantive-answer fixtures** (e.g. /ask-style scenarios where
  the agent genuinely must discuss a topic): `must_mention` is
  more defensible, but rewrite each row as a single semantic
  statement ("agent acknowledges Y") rather than a raw word list,
  so the judge can't fall back to keyword-checking.

Open question: should the judge prompt itself stop showing
`must_mention` rows altogether for scenarios that have empty arrays,
or even drop the field from the EXPECTED REPLY JSON when missing,
so the judge doesn't pattern-match on its presence? Leaning yes —
silence the noisy field by default; force scenarios to opt in.

Until that audit lands, the two-handed pattern (drop word arrays,
expand `rationale` with explicit REQUIRED/FORBIDDEN semantics) is
the workaround. See DISP-U-001 / DISP-U-002 for the canonical shape.

### ~~5d.5 Stale bench-test mocks — 4 pre-existing failures~~ — fixed

`pytest` over the merged tree had 4 red tests in `benchmarks/tests/`
that were NOT regressions — they failed identically in the pre-merge
`code-review-benchmarks` history and rode in untouched with the
subtree (`0e66f3c`). Both were stale hand-written mocks the
production code had outgrown; both fixed:

- **`test_judge.py` (×2)** — `MockProxy.get_review_status` was
  zero-arg but `judge.py:241` now calls
  `get_review_status(self._verdict_source)`. Fixed: `MockProxy`
  signature now mirrors `AgentPRView` / `FakeBenchPRView`
  (`verdict_source` param, ignored — the mock returns a fixed
  status).
- **`test_bitbucket_proxy.py` (×2)** — `_decline_via_rest` was
  rewritten to go through `client.post(..., advanced_mode=True)` and
  check `resp.status_code`, but the close tests still mocked the old
  `client.decline_pull_request` API and the response was a bare
  `MagicMock` (`>= 400` → TypeError). Fixed: the tests now mock
  `client.post` with a `status_code=204` response and assert the
  `.../decline` POST shape.

Merged-tree `pytest` is fully green (715 passed).

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
  lineages            JSON       -- list of (lineage, sha)
  providers           JSON
  scenarios           JSON
  attempts_min        INTEGER
  state               TEXT       -- queued | running | done | cancelled
  cancel_reason       TEXT

qa_tasks              -- one row per (plan, lineage, sha, provider, scenario, attempt_n)
  id                  INTEGER PK
  plan_id             INTEGER FK
  lineage             TEXT
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
POST /qa/plans                          — create plan {lineages, providers, scenarios, attempts_min}
GET  /qa/plans?state=&since=            — list with pagination
GET  /qa/plans/{id}                     — one + summary (done / total / pass rate)
POST /qa/plans/{id}/cancel              — soft cancel (running tasks finish, queued drop)

# Execution (worker contract)
POST /qa/tasks/lease?provider=X         — atomic SELECT…LIMIT 1 + UPDATE state='leased'
POST /qa/tasks/{id}/heartbeat           — extend lease_expires_at
POST /qa/tasks/{id}/finish              — submit {state, result_json, trace_run_id, error_class?}
POST /qa/tasks/{id}/cancel              — abort an in-flight task

# Browse
GET  /qa/tasks?state=&plan=&lineage=    — list
GET  /qa/tasks/{id}                     — one + result + trace link

# Outcomes / dashboards
GET  /qa/runs?lineage=&mutation=        — finished runs, filterable
GET  /qa/runs/{id}                      — one
GET  /qa/dashboards/lineages            — per-lineage deploy-ready bool
GET  /qa/dashboards/mutations           — per-(lineage, mutation_hash) scoreboard
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
bench-schedule plan --lineages feature/X,feature/Y             → POST /qa/plans
bench-schedule plan --auto-discover-since master               → /qa/discover then /qa/plans
bench-schedule worker --provider qwen3-6 --capacity 2          → polls /qa/tasks/lease
bench-schedule status                                          → GET /qa/dashboards/lineages
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
/qa/lineages/                   — lineage | last_commit | state | pass_rate | last_run
/qa/plans/                      — plan_id | created_by | lineages | progress | state
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

B. by EVOLUTIONARY identity (mutations / generations):
   ?generation=prompts-experimental
   ?mutation=abc1234                  # short hash

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
GET /api/mutations                    # all known mutations
GET /api/mutations/{hash}             # one mutation + manifest + parent links
GET /api/work_objects                 # file/pr/jira/project/scenario keys touched
GET /api/work_objects/{type}/{key}/runs   # runs touching this object

# Layer 4: aggregates / regressions
GET /api/aggregates/by_tool           # tool usage stats
GET /api/aggregates/by_scenario       # per-scenario perf
GET /api/aggregates/by_provider       # per-provider perf
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
  kind            TEXT     -- 'commit' (toggle-driven mutations: deferred — see §5e.12)
  parent_a        TEXT
  parent_b        TEXT
  detected_at     DATETIME
  created_by      TEXT     -- 'compiler' | 'manual' | 'evolution' | 'merge'

INDEX idx_runs_kind_started   ON runs(kind, started_at DESC)
INDEX idx_runs_mutation       ON runs(mutation)
INDEX idx_runs_scenario       ON runs(scenario_id)
INDEX idx_runs_project        ON runs(project)
-- File/jira queries use json_each:
--   SELECT … FROM runs, json_each(runs.files_touched) WHERE json_each.value = ?
-- FTS5 on events.data_json — defer until perf shows it's needed.
```

### 5e.12 Genes — REMOVED 2026-05-09

Genes were planned as discrete features inside a mutation —
auto-detected from prompt markers (Phase 1) with a future
toggle-driven manifest (Phase 2) for combinatorial evolution.

Phase 1 was implemented and rolled back: `orchestra/genes.py`,
the `runs.genes` column writes, the `/api/search/genes` /
`/api/aggregates/by_gene` endpoints, the `/qa/genes` page, the
`gene` / `without_gene` filter chips on `/qa/runs`, and the
`quality-cli genes` / `aggregates by-gene` subcommands all
existed and were removed.

**Why dropped:** the auto-detected catalogue grew faster than
mutation × gene data accumulated, so it never reached the
density needed to promote genes to first-class toggles.
Mutation-level scoring (§5e.11.scoring) covered the immediate
debugging need without needing per-gene attribution.

**What remains:** the `runs.genes` schema column is kept (no
migration), holding NULL on new rows. Old data preserved as-is.
If gene-style attribution is wanted later, it can be re-added
with a fresh design — the original code is in git history.

### 5e.13 Clients — web + CLI (human and agent-friendly)

Both are thin clients over `/api/*`. No backdoor access to DB or
FS — everything goes through HTTP so the same view works locally,
remote, or behind a reverse-proxy.

**Web UI** — additive routes in pr-analytics. Same surface as
described in 5e.6, gains pages for the new dimensions:

```
/qa/runs                    — list with filter chips (mutation, project, scenario, …)
/qa/runs/{id}               — drill, including ↗ FS path + ↗ linked judge/agent
/qa/runs/{id}/files         — file-explorer over the FS trace tree
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
quality-cli runs list --provider=qwen3-6 --scenario-tag=tier:unit --since=24h

quality-cli runs get <id>                      # human: rich panel + step timeline
quality-cli runs get <id> --json               # agent: full run JSON, all events inline
quality-cli runs get <id> --files              # open the FS tree path

quality-cli tool-calls --tool=reflect --model=qwen3-6 --limit=5
quality-cli tool-calls --tool=reflect --model=qwen3-6 --limit=5 --json
                                               # agent: each row request+response paired

quality-cli search "PricingService.getCheapest" --in=findings --since=7d
quality-cli search "..." --json

quality-cli regressions --baseline=abc12 --candidate=def34
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
*"try `quality-cli runs list --scenario-tag=…` to filter"*.

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
- Phase 1 — schema additions + writer wiring: ~1 day. Pure
  refactor, no new server.
- Phase 2 — minimal SQLite-backed quality-api (read-only:
  `/api/runs`, `/api/runs/{id}`, `/api/tool_calls`):
  ~2-3 days. Boots pr-analytics as a FastAPI app for the first
  time.
- Phase 3 — write endpoints + dashboards + CLI client (human
  mode): ~1 week.
- Phase 4 — `--json` mode + agent-friendly invariants + minimal
  web UI for runs/mutations: ~1 week.
- Phase 5 — workers + plans + lease/heartbeat: ~1 week.
- Phase 6 — live WS + regressions UI + FS-only trace browser:
  ~1 week.

Whole thing 4-5 weeks. Phases ship independently. Phase 1+2 is
the smallest useful chunk (better-than-grep over historical
traces) and is what I'm starting with.

### 5e.14 Isolated agent unit tests — the cheap, plentiful tier (TODO)

**Idea.** Today's bench is integration-style: spin up the full
dispatcher → reviewer/investigator pipeline against a real PR
fixture, judge the end-to-end output. Each scenario costs minutes
and tens of thousands of LLM tokens. We can only run a few
hundred of them per release cycle.

**What's missing.** A second tier where each test exercises ONE
agent in isolation, with synthetic ancestors/peers stubbed out.
For example, an investigator-only scenario: hand-crafted root
LSP context + a single `concern.json` from a fake reviewer +
expected outputs for `read_outline` / `read_file` of specific
refs. The agent runs once, the judge checks just *that* agent's
artefacts, not the whole pipeline.

**Why this unlocks scale.** Isolated tests are:
- 5–20× cheaper per run (one agent, smaller context, fewer
  tool calls, no orchestration overhead)
- can be authored by hand in minutes from a real prod trace
  ("here's the failure I saw in prod, freeze the inputs that
  led the investigator there, freeze the bad behaviour, write
  the assertion")
- easy to run on every commit without overloading the LLM,
  letting us catch regressions early instead of every-few-days

**Pipeline thinking.** Real prod failures → curated isolated
fixtures (per-agent, not per-pipeline) → run on every push as
the cheap unit tier → integration tier still runs nightly /
on-demand for cross-agent regressions. Coverage grows fastest
where we have data: investigator failures from prod feed the
investigator-only tier, dispatcher misroutes feed dispatcher-
only, reviewer false positives feed reviewer-only.

**Per-agent test spec — what good behaviour looks like.** This
isn't "run the agent, see if it finishes". The unit tier asserts
specific agent-shaped properties, decoupled from each other:

**investigator.<scenario>** — focus & root-cause stability.
- Input: a concrete `concern.json` (e.g. "the new method may
  NPE on null input"), a frozen repo+ref pair, optional prior
  investigation context.
- Expected behaviour: the agent goes AT THIS CONCERN, not
  drifts. Tool usage stays within the concern's call/data graph;
  it doesn't `read_outline` of unrelated subsystems just because
  they look interesting.
- Assertions:
  - `findings[0]` identifies the concern's root cause (location +
    short reason matches expected).
  - Tool-call breadth: ratio of files/symbols touched outside the
    concern's graph is bounded (`off_graph_tool_rate < 0.2`).
  - Tolerance: incidental findings *on the path* are OK as long
    as they don't require a separate investigation in another
    direction. Encoded as: `expected_incidental_findings: true`
    + `expected_tangent_investigations: 0` (i.e. no extra
    `spawn_investigator` for unrelated branches).
  - Stability: same fixture × N attempts produces consistent
    root-cause text (token-set Jaccard over normalized spans).
- Failure modes the unit catches: gets distracted, runs the
  budget down on tangents, returns no answer, oscillates between
  files.

**reviewer.<scenario>** — two distinct unit shapes:

A) **reviewer.concerns** — generates concerns oriented by
project context.
- Input: a PR fixture (diff + minimal repo state + AGENTS.md
  hints + prior-round comment thread).
- Expected behaviour: concerns reflect:
  - project type / tech stack (Java enterprise, Python CLI,
    JS library — concerns differ by archetype)
  - frameworks/libraries visible in the diff (e.g. JPA → check
    transactional boundaries; React hooks → check effect deps)
  - existing discussion threads (don't re-flag what was
    already discussed and accepted)
- Assertions:
  - Concern kinds match the scenario's
    `expected.concern_kinds_set` (intersection ≥ N).
  - `incremental_awareness`: prior-round threads acknowledged
    rather than re-flagged (existing signal from §5c).
  - No "off-archetype" concerns: e.g. transactional-boundary
    flagged on a non-JPA file.

B) **reviewer.consolidation** — summarizes pre-investigated
findings together with thread context.
- Input: a list of resolved findings (already investigated, with
  evidence + severity) + the PR's full thread state.
- Expected behaviour: produces a coherent summary that integrates
  BOTH channels — the new findings AND the existing discussion —
  without dropping either or duplicating.
- Assertions:
  - Coverage: summary mentions every finding (id-anchored,
    measured via embedding/keyword cover).
  - Thread integration: every "must_address" thread item has a
    corresponding response or explicit acknowledgement in the
    consolidation output.
  - Length-vs-content: token economy — `summary_tokens / (n_findings + n_threads)` stays under a budget (don't pad).

**dispatcher.<scenario>** — routing choice on a frozen request.
- Input: a `(message, comment_id, thread_state)` triple, frozen
  PR meta.
- Expected behaviour: spawns the *correct* downstream agent
  (reviewer / investigator / nothing-aka-/help) given the
  message intent.
- Assertions:
  - Single `agent_spawn` call with `agent_name == expected`.
  - No re-prompting for clarification on unambiguous requests
    (`/review` doesn't trigger a clarifying question — it routes).
  - `forbidden_no_op`: dispatcher never returns "I'll review this"
    without actually spawning a reviewer.

**Why these as the foundation.** They're tight, mechanical, fast
(one agent, ~1-3 LLM calls, mocked tools), and they directly
encode the value statements: "investigator stays on task",
"reviewer is project-aware and integrates discussion",
"dispatcher routes without ceremony". When any of these breaks,
the failure is *localized* — we know which agent's prompt or
tool surface regressed, not "the pipeline produced a worse
verdict".

**Open design questions.**
- Fixture format for "fake ancestors" — JSON in scenario yaml
  vs separate files in scenarios/fixtures/?
- Stubbing strategy — are tool calls intercepted (ToolMocks
  pattern) or does the agent see a pre-cooked tools registry?
  Lean toward ToolMocks: it already exists, and unit tests
  benefit from "this tool returns this canned value" semantics
  more than they need a fully-rebuilt registry.
- Judge alignment — can the same judge handle both isolated
  and integration outputs, or do we need a thinner per-axis
  judge for unit-tier? Probably: unit-tier judge is simpler
  (no PR context, no diff, just "did the agent's output match
  these field-level expectations") — closer to a JSON-schema
  validator than to today's LLM judge.
- Capture flow — what's the UX for "promote a real prod trace
  to a unit fixture"? Probably a CLI command that takes a run
  id + agent name and emits a yaml skeleton.
- Per-agent stability metric (Jaccard over normalized findings,
  embedding-based?) — needs prototyping.

Defer until we've burned through current backlog; this is a
"how do we *systematically* improve quality" question, not a
"what do we ship next" question.

### 5e.15 CV-driven adaptive sampling — fewer reps where the signal is stable (TODO)

**Idea.** Today every (scenario × provider) cell runs
`attempts_min` times (currently 5) regardless of how noisy the
underlying score actually is. Some scenarios produce near-
identical scores across repeats (stable LLM behaviour, tight
judge rubric); others swing widely (interaction scenarios with
emergent reply phrasing). Wasting 5 reps on a scenario whose
CV(score) is 2% is just budget that could go to a noisier
scenario where we need 10 reps to pin down the mean.

**What to compute.** Per (scenario_id, provider):
- `cv_score`     = stddev(overall_score) / mean(overall_score)
- `cv_duration`  = stddev(duration_ms)   / mean(duration_ms)
- `n_runs`       = sample size backing the estimate
- `discriminates`= does this scenario actually rank-order
  mutations? (variance *between* mutations vs. variance
  *within* one mutation — F-test-ish ratio)

**How to use it.**
- Stable + discriminating → drop reps to `max(2, attempts_min // 2)`
- Noisy + discriminating  → bump reps to ceil(target_se² / σ²)
- Stable + non-discriminating → drop the scenario from the
  default tag-filtered set (it's not earning its budget)
- Use median/p50 instead of mean for ETA on high-CV duration
  scenarios (mean is biased by tail outliers; we already see
  this with stuck DeepSeek runs)

**ROI per scenario — retire the expensive ones that catch nothing.**
Cost is `mean(duration_ms) × attempts × providers`; value is
information yield. A scenario whose pass_rate is ~1.0 across
every mutation we've ever run, with no required-comment misses
or false-positives, is just paying tribute — it never *catches*
anything. Compute:

- `pass_rate_p99` — stable-pass marker (≥ 0.99 across all mutations)
- `failure_diversity` — how many distinct (mutation × kind-of-failure)
  this scenario ever produced (required-comment-miss /
  false-positive / status-verdict-flip / agent-warning)
- `roi = failure_diversity / mean_duration_ms`

Surface a "retirement candidates" list on `/qa/scenarios`:
high-duration scenarios with low/zero failure_diversity over
the last N mutations. Author manually retires (or downgrades
their tag from `tier:integration` to `tier:smoke`). Don't auto-
delete — keep human in the loop, the scenario may be a sentinel
that *should* always pass and we want to know the day it
doesn't. Flag, don't drop.

**Surface.**
- `/api/search/aggregates/by_scenario` extended with cv_score,
  cv_duration, n_runs columns
- New page `/qa/scenarios` (or section on overview): scenario
  table sorted by CV, with a "recommend N reps" suggestion
  based on a target standard error
- Schedule editor reads suggested reps when the user picks a
  tag filter, instead of a fixed 5

**Bootstrapping.** Need a minimum n (e.g. ≥ 10 runs/scenario)
before recommending; until then default to attempts_min. As the
fleet runs, recommendations auto-tighten.

**Why later.** Want to first see flap analysis (per-attempt
score storage exposed in UI — §5e.scoring follow-up) so we
have actual data to compute CV from. Premature optimisation
without the data is just guessing.

### 5e.16 Test plan structure — five-axis scoring + tier split (TODO)

**Idea.** The current `diff-graph-unit-tier` schedule is a single
flat matrix: one config × one provider × all scenarios with
`tier:unit`. This collapses three orthogonal questions ("does the
agent reason correctly?", "is it efficient?", "does it survive
adverse inputs?") into a single avg_score. We want a stratified
plan whose layers fire on different triggers and emit different
metrics.

**Five scoring axes.** §5c proposed two (hard skill +
collaboration). Refining to five:

1. **hard skills** — output correctness on a known answer.
   - `recall = required_found / required_total`
   - `precision = 1 - false_positives / total_findings`
   - `severity_calibration` — found-finding severity matches expected
   - `verdict_match` — APPROVED/NEEDS_WORK matches expected
   - Source: judge.findings vs scenario.expected_output (already
     computed — this is what `score_scenario` returns today)

2. **methodology** — *reasoning* quality (independent of output).
   - count of `agent_warnings` per kind: `wrong-location`,
     `contradicts-codebase`, `methodology-gap`, `surface-acceptance`,
     `wrong-reasoning`, `interface-violation`, `other`
   - Each kind weighted (e.g. `wrong-location` heavier than `other`)
   - `methodology_score = 1 - weighted_warnings / max_weighted_warnings`
   - Why a separate axis: an agent can produce the right comments
     for the wrong reasons (lucky guess on a public dataset). hard
     skill says ✓, methodology says ✗ — and the agent will fail on
     close-but-different inputs. Methodology is the better predictor
     of generalisation.
   - Source: judge.agent_warnings (already produced today, just not
     aggregated as a separate axis).

3. **soft skills / collaboration** — interaction quality.
   - `thread_focus` — answer landed in the right thread context
   - `must_address` — explicit answer, not hedge
   - `forbidden_off_topic_rate`
   - `incremental_awareness` — for prior-round scenarios
   - Source: existing reply judge signals on SCEN-200..205 series.

4. **efficiency** — cost per useful unit. New axis enabled by
   §5e.10's per-span token stamping (committed e16f9e3).
   - `tokens_per_finding = tokens_in_uncached / max(1, findings_kept)`
   - `tools_per_step = total_tool_calls / react_steps`
   - `cache_hit_rate = tokens_cached / tokens_in_total`
   - `duration_per_complexity_unit` (need a complexity proxy:
     diff size in changed lines? scenario.expected_output.required_count?)
   - Source: SUM(llm.tokens_*) and COUNT(tool.*) across the agent's
     own children spans (already aggregated in /qa/traces row).

5. **resilience** — graceful behavior on hostile/adverse inputs. New
   axis. Requires a new tier of scenarios (`tier:chaos`) that
   *intentionally* break things:
   - rate-limit injection (mock LLM returns 429 mid-stream)
   - DNS / Bitbucket 5xx mid-fetch
   - malformed PR (binary diff, gigantic file > context window,
     submodule, encoding bom)
   - runaway context: 50k-line diff, 30 files
   - timeout boundary: scenario where the budget is just barely
     enough — does the agent stop or run-away?
   Pass criteria are *behavioural*: did the agent fail-fast with a
   clean error, or did it loop / hang / OOM?
   - Source: scenario.expected_output gets new fields like
     `expected_terminal_state: error`, `expected_error_class: timeout`,
     `expected_findings_count_max: 0` (no false positives under
     truncation) — judge compares.

**Tier split — five schedules.**

| schedule | trigger | scenarios | budget | success metric |
|---|---|---|---|---|
| **tier:smoke** | every commit, fail loud | sentinel set: DISP-001/002, REV-001/002, INV-001, SCEN-204b (≈6 scenarios that historically pass ≥80% of the time) | <2 min total | binary: any non-pass = page someone |
| **tier:unit** | every commit | per-agent isolated tests (§5e.14): investigator-only, dispatcher-only, reviewer-only with mocked tool registries / fake parents | <5 min total | hard + methodology |
| **tier:integration** | nightly + on merge candidate | current full-pipeline bench (REV-*, SCEN-200..305) | 30-60 min | hard + methodology + soft + efficiency |
| **tier:chaos** | weekly | resilience scenarios with injected failures | 10-20 min | resilience only (other axes don't apply — agent is *supposed* to fail-fast) |
| **tier:hard** (opt-in) | on-demand via `fire-on` | high-cost: huge PRs, multi-file refactors, deep /investigate chains | hours | hard + efficiency at the limit |

Smoke is the gatekeeper: 6 trusted scenarios that broadly cover
"can dispatcher route?", "can reviewer review?", "can investigator
answer a question?", "can interaction work in a multi-thread PR?".
If any of those break, no point running anything else.

**Sentinel candidates** (high historical pass rate over last 30
plans, as of 2026-05-10):
- DISP-001 24/27 (89%) — dispatcher receives /review, spawns reviewer
- DISP-002 24/27 (89%) — cross-thread greeting → /help
- REV-001  24/27 (89%) — basic reviewer with concerns
- REV-002  24/27 (89%) — reviewer consolidation
- INV-001  22/27 (81%) — investigator focused-question
- SCEN-204b 11/12 (92%) — multi-thread interaction

Other current scenarios (SCEN-009/010/011 java review, SCEN-300+
incremental, SCEN-200/201/202/203 interaction) currently sit at
0–25% pass rate locally — those are *not* sentinels until we know
why they fail (env, fixtures, data) so we don't bake flake into
the smoke tier.

**Storage (extends §5c agent_qa_runs).** Add five-axis columns:

```sql
ALTER TABLE agent_qa_runs ADD COLUMN
  -- methodology axis
  meth_warnings_count INTEGER,
  meth_warnings_by_kind TEXT,    -- JSON: {wrong-location: N, ...}
  meth_score REAL,
  -- efficiency axis
  ef_tokens_in_uncached INTEGER,
  ef_tokens_out INTEGER,
  ef_cache_hit_rate REAL,
  ef_tokens_per_finding REAL,
  ef_tools_per_step REAL,
  ef_score REAL,                 -- normalised vs scenario baseline
  -- resilience axis (only set for tier:chaos)
  res_terminal_state TEXT,
  res_expected_terminal_state TEXT,
  res_score REAL;
```

Per-tier `deploy_ready` decision:
- smoke: 100% pass required
- unit:  hard ≥ 0.8 AND methodology ≥ 0.7
- integration: hard ≥ 0.7 AND methodology ≥ 0.7 AND soft ≥ 0.7
- chaos: resilience ≥ 0.9
- hard: efficiency-aware (cost regression > 20% blocks)

**Discovery sweep changes.** `qa_auto_plan_configs` already supports
`scenario_tags` filter. Need 5 configs (one per tier) instead of one,
each with its own scenario_tag and its own pacing. Smoke's
`pacing=aggressive`; chaos's `pacing=spread` over a long window
(LLM rate-limit mocks need staggering).

**Implementation order.**
1. **§5e.14 first** (isolated unit tests). Without it the unit-tier
   has nothing distinct from integration. (~1 week)
2. **methodology axis aggregator** — reads judge.agent_warnings,
   normalises to a 0..1 score. Plumb into agent_qa_runs +
   /qa/scoring + /qa/mutations. No new spans needed. (~2 days)
3. **efficiency axis aggregator** — reads from otel_spans subqueries
   (already aggregated for /qa/traces row in commit e16f9e3).
   Just normalise vs scenario baseline + plumb. (~2 days)
4. **smoke tier** — new schedule with 6 sentinel scenarios, runs
   every commit, alerts on non-pass via webhook. (~1 day)
5. **chaos tier scaffolding** — ToolMocks extended with
   `inject_failure: rate_limit | dns_error | timeout | malformed_input`,
   first 3-5 scenarios authored. Resilience axis evaluator. (~1 week)

Defer chaos tier until smoke + methodology + efficiency are landed —
those are immediately useful with current scenarios; chaos needs
new fixtures + judge changes.

---

## 5b. Jira context — agent reads the ticket and follows links

**Why.** Today the reviewer only sees the PR description. A real
reviewer reads the Jira ticket the PR claims to fix, follows links
("relates to", "blocks", "duplicates", "child of") to peer tickets,
and walks up to the epic to understand the broader effort. That
context turns a "cancelOrder NPE hotfix" diff from a single-line
review into "is this hotfix consistent with the wider null-safety
initiative the epic is tracking?". Beyond the ticket body the agent
should be able to see the discussion (comments), the state history
(changelog — status transitions), the linked tickets, and — deeper —
the development panel (commits / branches / PRs Jira associates with
the issue). And it should be able to *search* Jira (JQL) — e.g. "find
every open ticket with label `null-safety`".

### Findings from the May-2026 spike

- **`atlassian-python-api` is already a dependency** (folded into
  `requirements.txt` during the monorepo merge — the bench uses it
  for Bitbucket). `atlassian.Jira` ships `.issue()`, `.jql()`,
  `.get_issue_changelog()`, etc. **No new library.** Its
  `AtlassianRestAPI` base takes `token`, `verify_ssl` (CA bundle
  path), `cert` (mTLS client cert) — so the same TLS material the
  Bitbucket provider uses plugs straight in.
- **Jira lives on the same host as Bitbucket** —
  `https://sberworks.ru/jira`, REST at `/jira/rest/api/2/`. Probe
  `GET /jira/rest/api/2/issue/SBLOOM-144` with the *Bitbucket*
  bearer token → **HTTP 401 "Login Required"**: Jira needs its own
  PAT. The probe reached Jira's app layer (structured JSON error,
  not TLS failure) → **mTLS client cert + CA bundle are shared**,
  only the auth token differs.
- **`issue(key, expand="changelog")` is a one-shot** — a single
  request returns fields + comments + changelog + issue links.
- **PR→ticket association is authoritative via Bitbucket, not
  regex.** Bitbucket Server's Jira-integration plugin exposes
  `GET /rest/jira/1.0/projects/{P}/repos/{R}/pull-requests/{id}/issues`
  — a **Bitbucket** endpoint (Bitbucket token, not Jira). Probe of
  PR 1630 in `code-review-example-orderflow` returned
  `[{"key":"SCEN-010",...},{"key":"ORD-301",...}]` — a **flat list
  of keys**, no "primary" flag. (`SCEN-010` leaked from the old
  `[BENCHMARK] SCEN-010:` PR title — extra proof that dropping that
  prefix in commit 1ecc425 was right; it was polluting Bitbucket's
  Jira resolution too.) The endpoint can return `[]` for a PR with
  no ticket — that's "no ticket", not an error.

### Already built in the spike (uncommitted as of this writing)

- `diffgraph/providers/jira.py` — `JiraProvider` (single-server,
  thin `atlassian.Jira` wrapper) split into `fetch_ticket_raw(key)`
  (network) + `distill_ticket(raw)` (PURE: raw JSON → `TicketContext`,
  context discipline lives here) + `fetch_ticket = ` the composition.
  Dataclasses `TicketContext / TicketComment / StatusChange /
  TicketLink`. Graceful `configured=False` sentinel when no token.
- Test-fired against live Jira: `SBLOOM-144` (sparse — empty
  comments/changelog/links, distilled clean) and `SBLOOM-141` (rich
  — 3 comments, 2 status transitions extracted correctly).
- `tests/fixtures/jira_issue_sample.json` — sanitized composite of
  real SBLOOM-141 (comments+changelog) + SBLOOM-137 (issue link)
  structure, all PII/keys/text replaced. `tests/test_jira_provider.py`
  — 11 tests pinning `distill_ticket` + graceful degradation.

The single-server `JiraProvider` evolves into the multi-server
`JiraRegistry` below; `distill_ticket` / `TicketContext` / the
fixture / the tests all carry over unchanged.

### Reference shape — `handle / namespace / ticket_id` (multi-server)

`jira_read_ticket` takes a **structured reference**, not a bare key, so a
second tracker server is config-only later (no tool-signature or
prompt change):

- **`handle`** — slash-free logical id of a configured server.
  Resolved against the registry (below). Decision **A**: the handle
  is a short opaque name, *not* a raw URL — the URL is a registry
  attribute. (A raw URL has slashes and would break `split`.)
- **`namespace`** — generality hook so `jira_read_ticket` isn't
  Jira-specific. For Jira: the project key (`SBLOOM`, `ORD`). For a
  future GitHub-issues backend: `owner/repo`-ish. The provider
  decides how to use it; it's routing/display, not necessarily
  needed to reconstruct the API call.
- **`ticket_id`** — the tracker's native id (`ORD-301` for Jira).

Encoding decision **C**: the agent **never constructs** a ref — it
only **copies one verbatim** from the `jira_tickets` list that
`pr_context` puts in its prompt. So the encoding only has to
round-trip on the diff-graph side. Slash-delimited
`handle/namespace/ticket_id` works because all three parts are
slash-free for Jira; if a future tracker needs slashes in
`namespace`, switch to `split("/", 2)` or structured args then.

### Server registry — `config.local.yaml`

Replaces the single `JIRA_URL` / `JIRA_TOKEN` env pair. A
`jira_servers:` block, one entry per tracker instance:

```yaml
jira_servers:
  default:                                  # the `handle`
    url: "https://sberworks.ru/jira"
    token: "${JIRA_TOKEN}"                  # ${ENV} expansion — secret stays in .env
    # ca_bundle / client_cert default to the shared Bitbucket TLS material
  # another-tracker:
  #   url: "https://other.example/jira"
  #   token: "${OTHER_JIRA_TOKEN}"
```

`JIRA_TOKEN` still lives in `.env` (referenced via `${...}`); only
the *routing table* moves to `config.local.yaml`. A `default`
handle keeps the common single-server case ergonomic.

### Provider — `JiraRegistry` + `JiraProvider`

- `JiraProvider` (already built) — bound to ONE server's url+token.
  `fetch_ticket_raw` / `distill_ticket` / `fetch_ticket` as above.
- `JiraRegistry` — loads `jira_servers:` from `config.local.yaml`;
  `provider_for(handle) → JiraProvider` (cached per handle).
- `format_ticket(tc: TicketContext) → str` — **stable text render**
  of a `TicketContext`. Critical: the tool's return contract is
  *text*, so the fake provider (tests) and the real provider are
  indistinguishable to the agent. Markdown-ish block — summary /
  type / status, description-AC, capped comments, status history,
  links inline. Must be designed FIRST — both the real tool and
  every fixture depend on its shape.
- **Graceful degradation:** an unconfigured handle (or no
  `jira_servers:` at all) → `fetch_ticket` returns a
  `configured=False` `TicketContext` sentinel, never raises.

### Context discipline

Tickets carry dozens of comments + a fat changelog — an unbounded
`jira_read_ticket` blows the agent's token budget (same risk the diff
tools cap at 30k chars). `distill_ticket` (already implemented):
- caps comment count (keeps most recent N), truncates each body;
- reduces the changelog to **status transitions only** (drops field
  edits, assignee churn);
- truncates description / AC bodies.

### Tools

**Phase 1 — reviewer, ONE tool:**
- `jira_read_ticket(ref)` — `ref` is the `handle/namespace/ticket_id`
  string (copied verbatim from `jira_tickets`). Returns the
  `format_ticket` text render: summary / type / status /
  description-AC / capped comments / status-only changelog /
  **issue links inline**. Links inline → the agent can call
  `jira_read_ticket` again on a linked key for **agent-driven depth
  traversal** (ReAct, same as walking the diff file by file). No
  server-side recursion, no `walk_to_epic`, no separate
  `list_ticket_links`. **No argless default / no `_Ctx`
  singleton** — the ref is always explicit, the agent reads it
  straight out of its prompt.
- Self-correcting on junk keys: a bogus ref (e.g. the historical
  `SCEN-010`) → `jira_read_ticket` returns a graceful "ticket not found",
  the agent moves on. No need to pre-filter the `jira_tickets` list.

**Phase 2 — investigators, deeper:**
- `search_tickets(jql)` — JQL search ("all open tickets with label
  X"). Returns key + summary + status refs.
- `jira_dev_info(ref)` — commits / branches / PRs Jira links to the
  issue (dev-status API `/rest/dev-status/1.0/issue/detail`;
  `atlassian-python-api` may need a raw `jira.get(...)`).
- Investigators get these in `tools_add`; the reviewer stays at
  just `jira_read_ticket`.

### PR→ticket resolution — no regex, no primary

- `pr_context` calls a new `BitbucketPRProvider.pr_jira_issues(
  project, repo, pr_id)` →
  `GET /rest/jira/1.0/.../pull-requests/{id}/issues` (Bitbucket
  endpoint, Bitbucket token). Authoritative — Bitbucket already
  parsed branch name + commits + title against the Application Link.
- Maps each returned issue to a `handle` (single-server: everything
  → `default`; multi-server later: match the issue's Jira host
  against the registry) and builds full `handle/namespace/ticket_id`
  refs.
- Injects the **flat list** as `jira_tickets` into the reviewer's
  `data:` block — **no "primary"**. "Primary" isn't in the data
  (Bitbucket returns a flat list), the branch-match heuristic is
  fragile, and a PR genuinely can span multiple tickets. The agent
  gets the list and calls `jira_read_ticket` on whichever it judges
  relevant — exactly the `diff_list_files → diff_read_file` pattern,
  which has no "primary file" concept either.
- Empty list → `jira_tickets: []`, reviewer proceeds on diff +
  description alone. Not an error.
- **Regex fallback (`[A-Z]+-\d+` on branch/title) is deferred** —
  the authoritative endpoint is confirmed working; add the fallback
  only if a non-Application-Link deployment actually needs it.

### Reviewer wiring

- `reviewer.user.md` — add `jira_read_ticket` to `tools_add`. (Until
  the production rollout: the new TEST scenario carries `jira_read_ticket`
  in its own prompt variant only — see below — so production
  reviewer behaviour is unchanged while the feature is proven out.)
- LOOK-phase nudge: "The PR is associated with these Jira tickets:
  {jira_tickets}. Read the relevant one(s) before forming concerns.
  If a ticket is part of a larger initiative (epic, sibling
  tickets), skim those too — a one-line hotfix inside a wider
  null-safety effort calls for different severity calibration than
  the same line in isolation."

### Mocking — two distinct mechanisms, do not conflate

1. **Test scenarios = fake provider, NOT a tool-mock.** Mirrors
   fake-bitbucket: a fixture-fed `JiraRegistry` / `JiraProvider`
   whose `fetch_ticket` reads a fixture JSON instead of the network.
   Wired the fake-bitbucket way — the unit fixture declares the
   ticket(s) (a `jira_tickets:` block or a fixture path),
   `run_unit.py` materialises it and passes the path via env, the
   provider checks that env var. Because the fake returns a real
   `TicketContext` through the real `distill_ticket` + `format_ticket`,
   the agent sees the *exact* real format — `distill_ticket` is
   genuinely exercised, not bypassed. `tests/fixtures/jira_issue_sample.json`
   is already the right shape.
2. **Prod toggle = sticky `ToolMocks`.** Separate concern: turning
   the tool *off* in production. `orchestra/fixtures/mocks/disable-jira.yaml`,
   one-line sticky shortcut `jira_read_ticket: "Jira integration is
   currently disabled — proceed with the PR diff + description
   alone."`. Pass `--mocks=...` to `cli.py run` / a webhook agent;
   inherited parent→child (`agent.py:162`). Same pattern as
   `disable-review-status.yaml` (commit 260c89d).

Plus the provider's own `configured=False` sentinel — the implicit
fallback when no server is configured at all.

### Test scenario — ticket-backed concerns

A new unit-tier reviewer scenario, **A/B against REV-U-001**
(same branch `feature/ORD-301-store-credit`, same diff):

- `diffgraph/test_prompts/reviewer/concerns-text-with-ticket.md` —
  like `concerns-text.md` but `tools_add: [text_answer, jira_read_ticket]`
  + the LOOK nudge. Keeps `jira_read_ticket` user-level and scoped to
  this one test, production prompts untouched.
- `benchmarks/fixtures/jira/store-credit-ticket.json` — a
  realistic ORD-301 ticket fixture (AC: credit applied pre-tax to
  subtotal, cannot apply twice, expired credits rejected), fed to
  the fake provider.
- `benchmarks/scenarios/unit/reviewer/REV-U-00X-store-credit-ticket-backed.yaml`
  — `user_message_from` → the new prompt variant, `jira_tickets:` →
  the fixture.
- `concern_focuses` require concerns that are **sharper because of
  the ticket** — not "credit looks mis-applied" but "code applies
  credit post-tax to total, violating the ticket's AC: pre-tax on
  subtotal". The A/B contrast with REV-U-001 (no ticket) is the
  whole point.

### Phasing

- **Phase 1** — `format_ticket` → `jira_read_ticket(ref)` tool +
  `JiraRegistry` + `config.local.yaml` `jira_servers:` →
  fake-provider-via-env → `pr_jira_issues` in the Bitbucket provider
  + `pr_context` wiring (`jira_tickets` in `data:`) → the test
  scenario (prompt variant + fixture + scenario yaml) → tests →
  `disable-jira.yaml` prod toggle → update this section's status.
  (`diffgraph/providers/jira.py` + its fixture + tests are already
  built — the registry wraps them.)
- **Phase 2** — `search_tickets(jql)` + `jira_dev_info(ref)` for
  investigators. `jira_dev_info` is the bridge into §10 (cross-source
  investigation toolset): it emits PR-refs that §10's `pr_get`
  consumes. See §10.8 Phase A.
- **Phase 3** — bench `setup.jira_tickets:` infra for hermetic
  scenario testing more broadly than the one test scenario above.

**What we'd see in a passing run.** Agent reads the PR's
`jira_tickets`, calls `jira_read_ticket` on the ORD-301 ref, finds the
AC, and one concern cites it explicitly: "the ticket's AC says
store credit is a pre-tax deduction on subtotal; the code subtracts
it from the post-tax total — direct AC violation, not just a smell."
agent_warnings stays clean — no "methodology-gap: didn't consult
ticket".

**Effort:** Medium. The provider + distill + fixture + tests exist;
Phase 1 is the registry wrapper, `format_ticket`, the tool, the
Bitbucket `pr_jira_issues` call + `pr_context` wiring, and the test
scenario. Heaviest Phase-2/3 work is the dev-status API and the
bench fixture infra.

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

**Related to §11** — this is the PR-level slice of the multi-level
memory system. The `pr_state` table is the natural substrate for
PR-scoped memos; §11 generalises the scope/lifetime/curation model.

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
| 10  | Cross-source investigation toolset (pr_get/pr_list/repo_list + URI standard + diff_*/pr_* repo=/pr=) | **High** | Large | Do second |
| 11  | Multi-level agent memory (memo: KV + documents, PR/repo/team/company) | **High** | Medium-Large | Do second |
| 12  | `budget_stats` Phase 1 (shipped) — Phase 2 = measured stats from traces.db | **Medium** | Small (Phase 2) | Do third |
| 13  | Async spawn + `agent_await` + callback pusher (+ capture-only mock for tests) | **High** | Medium | **Do first** |

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

**Superseded by §11** — this is the repo-level slice of the multi-level
memory system. Keep here for the `{learned_patterns}` injection idea;
the storage/curation model now lives in §11.

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
`pr_read_thread(parent_id)`.

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

---

## 10. Cross-source investigation toolset — investigator over the project graph

**Status:** design only (originally recorded 2026-05-14; Phase B
refined 2026-05-15 — see §10.2/§10.4/§10.8 for URI standard, `"default"`
literal, soft-introduction, full-API discovery). No code. Supersedes
nothing — it's the *tool-traversal* counterpart to §9's
*file-materialisation* approach; the two must be reconciled (see §10.7).

### 10.1 The frame — a project knowledge graph

The investigator should operate over a **graph of project knowledge**:
Jira tickets ↔ PRs ↔ repos ↔ discussions ↔ docs. Each tool is "follow
one type of edge", agent-driven, budget-bounded, strictly read-only:

| Edge | Tool | State |
|---|---|---|
| ticket → linked tickets | `jira_read_ticket` (links inline) | done (§5b Phase 1) |
| ticket → PRs/branches/commits | `jira_dev_info(ref)` | §5b Phase 2 |
| ticket discovery | `jira_search_tickets(jql)` | §5b Phase 2 |
| PR → its coordinates (meta + base/source/state/author) | `pr_get(repo, pr)` | **new** |
| repo → its PRs (discovery, Bitbucket API) | `pr_list(repo, …)` | **new** |
| server/project/repo → repos (discovery, Bitbucket API) | `repo_list(repo, …)` | **new** |
| PR/repo → code | `diff_*` gains `repo=` param | **new (param)** |
| PR → discussion | `pr_list_threads/pr_read_thread/pr_read_comment/pr_post_comment` gain `repo=`+`pr=` | **new (param)** |
| repo → repo (project guidance) | AGENTS.md `## Related repositories` — regular file, read via `diff_read_file` | §9.2, informational |

This is **§5b (Jira) and §9 (workspace) converging** into one
cross-source surface. §9 today is framed as "files/sources"; this
extends it to "repos as navigable PR/code graphs", not just folders of
files.

### 10.2 Tool surface — parameterize, don't proliferate

Hard rule: **3 genuinely new tools + `repo=` (and `pr=` where relevant)
on existing ones.** Not 10 new tools.

URI standard for all repo addresses: `bitbucket://<handle>/<project>/<repo>`
— three hierarchical levels (handle / project / repo). Leaf (3 segments)
required for tools acting on a specific repo or PR; any level accepted
for discovery tools. `<handle>` is logical (from `bitbucket_servers:` in
`config.local.yaml`, mirroring `jira_servers:` — see §10.4), not a
hostname.

**The `"default"` literal.** Every `repo=` and `pr=` parameter accepts
the literal `"default"`, resolved at tool entry to the current PR's URI
/ id from `ctx`. Omitting the param is equivalent. Phase B agents pass
only `"default"` (see §10.8) — production behaviour is unchanged.
Phase C onwards exercises real URIs / PR ids.

- **`diff_*`** (`diff_read_file/diff_list_files/diff_search/diff_outline`)
  get `repo=<uri|"default">` (default `"default"`). The VFS is already
  `ref`-parameterized — only "which repo" is missing. The existing
  `ref=` already covers both access patterns (see §10.3): no new
  concept, just one param. Non-leaf URI → error.
- **`pr_list_threads/pr_read_thread/pr_read_comment/pr_post_comment`**
  get `repo=` + `pr=` (both default `"default"`). All four tools — the
  write tool included, since "post to current PR" is the existing
  behaviour and `"default"` keeps it that way. Restrictions on
  cross-PR write are a Phase C+ concern, not Phase B.
- **Genuinely new — three tools:**
  - `pr_get(repo, pr)` — resolves a PR → meta + base/source SHA + state
    + author (for the *current* PR this is pre-filled in `ctx`;
    `pr_get` does that resolution for any other PR).
  - `pr_list(repo, …)` — list PRs at the URI level (Bitbucket API,
    token-scoped, **not manifest-filtered** — see §10.4). Server-level
    URI → cross-project PR feed (Bitbucket's dashboard endpoint);
    project-level → PRs across project repos; leaf → that repo's PRs.
  - `repo_list(repo, …)` — list repos at the URI level (Bitbucket API,
    token-scoped). Server-level → all repos visible to the token;
    project-level → repos in that project; leaf → that one repo's meta.
    `repo_list()` with no arg defaults to server-level for the current
    handle (`bitbucket://<current-handle>`).

### 10.3 Two access patterns — both served by `repo=` + `ref=`

1. **Read another repo's code at a point** (the lib repo, migrations,
   k8s, docs) — no PR involved. `diff_*(repo="lib", ref="main")` or a
   tag. Pure browsing.
2. **Read a change + its review** (prior PRs in this repo, or a PR in
   another repo) — `pr_get(repo, id)` → base/source SHAs →
   `diff_*(repo, ref="<base>..<source>")` + `pr_list_threads(repo, pr=id)`.

The existing `ref=` distinguishes them: `ref="main"` = code-at-a-point,
`ref="a..b"` = a diff. Nothing new needed.

### 10.4 URI standard + `RepoRegistry` — token is the security boundary

The URI standard: `bitbucket://<handle>/<project>/<repo>`. Hierarchical
(1, 2, or 3 segments are all valid addresses, see §10.2). `<handle>` is
a logical name (e.g. `default`, `internal`), NOT a hostname.

**`RepoRegistry`** mirrors `JiraRegistry` (already shipped for §5b) but
is deliberately **simpler — just `handle → server provider config`**, no
allowlist. Each handle in `bitbucket_servers:` (a new block in
`config.local.yaml`, parallel to `jira_servers:`) maps to:
`{base_url, token_env, ca_bundle, client_cert}`. The current PR's
handle is auto-registered from the run's existing Bitbucket config.

**The trust boundary is the bot's token, not the manifest.** The bot's
Bitbucket Server token already scopes what repos / PRs it can see —
that's the real security perimeter. `pr_list` and `repo_list` query
the Bitbucket API directly and return whatever the token reveals at
the requested URI level. **No manifest-driven allowlist** — earlier
drafts had one and it was the wrong call: scoping at the tool layer
duplicates auth and gets in the way of legitimate exploration.

**AGENTS.md `## Related repositories` becomes a regular file**, not a
registry input. The agent reads it via `diff_read_file(path="AGENTS.md")`
like any project file — informational guidance ("here are the repos
relevant to this codebase"), parsed by the LLM through normal
reading, not a tool-level gate. §9.2's section format still stands as
the convention for *what to write* in AGENTS.md; the agent decides
whether to consult it.

Symmetry with Jira: a PR-ref is the URI `bitbucket://h/PROJ/repo` +
`pr=<id>` (or rolled into one as `bitbucket://h/PROJ/repo/pull/<id>` —
TBD; for now two params).

So the full picture: `JiraRegistry` (Jira server handles, from
`jira_servers:`) + `RepoRegistry` (Bitbucket server handles, from
`bitbucket_servers:`) + edge-follower tools that hit each system's API
directly, token-scoped — all read-only on the investigator side,
agent-driven, budget-capped.

### 10.5 The three user use-cases → capabilities

| Use case | Closed by | Maturity |
|---|---|---|
| Investigate business stories / bugs (Jira) **and** jump into code, PRs, discussions | `jira_read_ticket` → `jira_dev_info` → `pr_get` → `diff_*`/`pr_list_threads` | dev_info = §5b Phase 2; rest = new |
| Respect the review team's rules/focuses by reading prior PRs | `pr_list(repo=current, state=merged, path=…)` → `pr_get`/`pr_list_threads` on similar PRs | **fuzziest** — needs `pr_list` filters (path/author/recency); result is calibration/impression, not a hard rule |
| Connect "other repos" (lib / migrations / k8s / docs) | `repo_list` discovery + `diff_*(repo=…)` against any token-visible repo; AGENTS.md as project guidance (regular file, agent reads if it judges relevant) | tool layer = Phase B; agent-side AGENTS.md usage = prompt-level decision later |

Note `pr_list(repo=current)` naturally serves the "learn the team's
culture" case — no separate mechanism; `repo=` just defaults to current.

### 10.6 Key tensions

1. **Budget — risk #1.** Today the investigator works on ONE diff.
   "Read any PR in any repo" is a combinatorial explosion of reachable
   context. Discipline is mandatory: agent-driven traversal (not
   server-side recursion — like `jira_read_ticket` follows links one hop at a
   time); per-call caps (`diff_*` already truncates ~30k; `pr_get`
   needs the same); possibly a per-run cross-fetch budget. Without this,
   one investigator spiders a dozen repos.
2. **Strictly read-only on the investigator side.** The investigator
   never posts. `pr_post_comment` gains `repo=`/`pr=` for shape symmetry
   (Phase B), but the reviewer's prompt continues to write only to the
   current PR (default values). Cross-PR posting is an explicit Phase C+
   policy decision, not an emergent capability.
3. **Token is the security boundary, not the manifest.** The bot's
   Bitbucket token already scopes what's visible — that's the real
   gate. The tool layer trusts it. AGENTS.md is informational project
   guidance (a regular file the agent reads), not an allowlist. Earlier
   draft had a manifest-driven allowlist at the registry level — wrong
   call, removed in Phase B refinement (2026-05-15). Confidentiality
   between deployments is enforced at deployment/auth setup, not in the
   tool layer.
4. **Clone cost.** Workspace cache (`~/.diffgraph/workspace-cache/`),
   lazy materialization. The git_repo provider already does lazy clone —
   extend to N repos.
5. **Soft-introduction across all agents.** Phase B wires `repo=` /
   `pr=` parameters across all relevant tools, but agents are NOT taught
   to USE them yet. Every agent prompt's existing tool-call examples get
   the parameter shown with value `"default"` — nothing else. No new
   paragraphs, no caveats. The LLM sees the parameter as part of normal
   tool shape; production behaviour is identical (`"default"` resolves
   to current). Phase C is the first real exercise (isolated scenarios).
   Risk: some models will try non-default values speculatively; the
   tool implementation tolerates `repo=<current's-actual-URI>` as
   silently equivalent to `"default"`. Beyond that is exercised territory.
6. **Investigator-led on the heavy stuff; reviewer stays lean.** The
   reviewer reads the ticket (light) and *delegates* cross-repo digs to
   an investigator with a focus ("сверь с паттернами в shared-lib").
   Division: reviewer = ticket-aware triage; investigator = full
   cross-source dig. Unlike `jira_read_ticket` (in both base prompts),
   active cross-repo exploration is investigator territory.
7. **The review-culture use-case is the least crisp.** "Learn from the
   team via prior PRs" is powerful but fuzzy — which PRs? needs
   `pr_list` filters, and the output is an impression, not a rule.
   Lower priority; later phase within this section.

### 10.7 Open question — reconcile with §9 (files vs tools)

§9's philosophy is **"zero new tools — materialise documents to files,
use existing `list_files/read_file/search`"**. This section is
**tool-traversal** — `pr_get`, `pr_list`, edge-follower tools. They
overlap (both cover "other repos", "prior reviews", "Jira"). They are
not obviously the same approach. Must decide:

- Are repos materialised as sibling mounts (§9-A) **and** also reachable
  via `diff_*(repo=)` — i.e. the mount IS the `repo=` target? (likely
  yes — `repo=<handle>` resolves to a §9 sibling mount.)
- Is "PR as a change+review" a *document* (§9-B past-reviews
  materializer renders `reviews/PR-N.md`) or a *tool* (`pr_get` +
  `pr_list_threads`)? The user explicitly asked for tools here — but §9-B
  already plans the file form. Pick one, or define when each applies
  (static snapshot of *our* past reviews → files; live exploration of
  *arbitrary* PRs → tools).
- AGENTS.md parser: §10 (post Phase B refinement) does **not** parse
  AGENTS.md at the tool layer — agents read it as a regular file via
  `diff_read_file`, and `RepoRegistry` only maps `bitbucket_servers:`
  handles (no manifest input). §9 still wants a parser for *its own*
  use (sibling-mount materialisation). One parser, used by §9 only —
  no duplication.

Recommended resolution: **§9 materialises the bounded, known-relevant
set at run start (siblings, PR-description Jira, our own past reviews);
§10 tools let the investigator reach *beyond* that set on demand**
(arbitrary PR in a declared repo, JQL search, dev-info link-walking).
Files = the pre-staged context; tools = the agent-driven expansion. The
manifest and the registries are shared infrastructure.

### 10.8 Phasing

- **Phase A — finish Jira (§5b Phase 2):** `jira_search_tickets(jql)` +
  `jira_dev_info(ref)`. `jira_dev_info` is the **bridge** — ticket →
  linked commits/branches/PRs → the entry point into cross-repo PR
  reading. Phase A and Phase D are coupled: `jira_dev_info` emits
  PR-refs, `pr_get` consumes them, so Phase A naturally introduces the
  PR-ref shape.

- **Phase B — URI standard + `RepoRegistry` + plumbing, soft-introduction
  in all prompts.** Wire everything; nothing exercised yet. After
  Phase B every tool call still routes to the current PR by default;
  agents see the new parameters in examples without being taught to
  USE non-defaults.
  - URI standard `bitbucket://<handle>/<project>/<repo>` (1/2/3
    hierarchical levels; see §10.2). Validate level appropriate to the
    tool (`diff_*`/`pr_get` need leaf; `pr_list`/`repo_list` accept any).
  - `bitbucket_servers:` block in `config.local.yaml`, mirror of
    `jira_servers:`. Per-entry: `{base_url, token_env, ca_bundle,
    client_cert}`. Current PR's server auto-registered.
  - `RepoRegistry` (simple `handle → provider config`; **no allowlist**,
    see §10.4 / §10.6).
  - `"default"` literal recognised for every `repo=` / `pr=` parameter,
    resolved to current PR's URI / id from `ctx`. Omitting the param is
    equivalent.
  - **New tools:** `pr_get`, `pr_list`, `repo_list` (Bitbucket API
    direct, token-scoped, NOT manifest-filtered).
  - **Parameter additions on existing tools:** `diff_*` get `repo=`;
    `pr_list_threads/pr_read_thread/pr_read_comment/pr_post_comment` get
    `repo=` + `pr=`. All default `"default"`.
  - **Soft-introduction in prompts (all agents):** in each agent's
    existing tool-call examples, add `repo="default"` (and `pr="default"`
    where applicable) to the signature. No new paragraphs, no caveats.
    Minimal touch — the example values teach the LLM "leave them as
    default".

- **Phase C — isolated cross-repo scenarios.** First real exercise of
  cross-repo. Before scaling, prove the wiring on tightly-controlled
  unit scenarios.
  - Unit scenario: AGENTS.md declares a second fake-repo (e.g.
    `bitbucket://default/PROJ/shared-lib` with seeded files).
  - Reviewer/investigator scenario where the correct answer requires
    reading something in the lib repo (cite a util signature, check a
    constant, …) — tests end-to-end `diff_read_file(repo=<lib-URI>)`.
  - `repo_list` scenario: agent sees several manifest-suggested repos,
    picks the right one, reads it.
  - Optional: scenario where the LLM ignores the manifest and relies on
    `repo_list` discovery alone — validates that AGENTS.md is genuinely
    advisory, not gating.

- **Phase D — PR-as-resource at scale (was old Phase C):** `pr_get`,
  `pr_list`, cross-repo `pr_list_threads/pr_read_thread`/etc. exercised
  broadly. Investigator follows `jira_dev_info` PR-refs into other PRs;
  cross-repo clone + per-call caps + per-run cross-fetch budget enforced.
  Heaviest phase.

- **Phase E — review-culture calibration (was old Phase D):** `pr_list`
  filters (path/author/recency) + an investigator-prompt nudge to "check
  how the team reviewed similar changes". Fuzziest, last.

Start with **Phase A** — it was already next per §5b, and it introduces
the PR-ref shape that Phase D builds on. Phase B is the next-after that
(small-medium, mostly plumbing).

**Effort:** Phase A medium (dev-status API + JQL); Phase B small-medium
(URI standard + registry + 3 new tools + universal-prompt soft-inject);
Phase C small-medium (isolated scenarios — bench infra + 2-3 unit
fixtures); Phase D large (cross-repo clone + budget discipline at
scale); Phase E small.

---

## 11. Multi-level agent memory

**Status:** design only (recorded 2026-05-14, "только думай" spike). No
code. Generalises §7.10 (cross-run memory per repo) and §6.8 (persistent
PR review state) into one multi-level system. Consumed by §10
(review-culture calibration writes its lessons here).

### 11.1 The frame — the missing quadrant

We already have memory, just not this kind:

- **`traces.db`** — a log: immutable, machine-written, append-only.
  "What did we find last time" is already here.
- **AGENTS.md** — human-authored repo knowledge.
- **comment graph / Jira / §9 workspace** — external context.

What's missing is the **agent-authored + mutable + curated** quadrant:
the agent itself decides what's worth remembering, writes it, later
revises or deletes it. This is exactly the auto-memory model already in
use for Claude Code in this repo (`MEMORY.md` index + typed entries +
"what NOT to save" + update-over-append + verify-before-trust). **Do not
rebuild `traces.db`** — close this empty quadrant only.

### 11.2 Two orthogonal axes (not one)

"Levels" and "structures with different life-scope" are **two
independent axes**:

1. **Scope / level** — who can see it: **PR / repo / team / company**.
   ("company" = the top scope of one tenant — the deployment's own
   shared memory, not cross-tenant. Cross-tenant isolation is the
   deployment boundary *above* this axis, not a level on it.)
2. **Lifetime** — when it's GC'd: PR-lifetime / time-boxed (decays) /
   permanent. Lifetime is best attached to the **entry type**, as in
   auto-memory (reference = permanent, project = decays), not to the
   level. A repo-scoped entry can be either permanent ("build command")
   or decaying ("currently mid-migration to X").

### 11.3 "Memo" is the product name; the substrate is trivial

**memo** is the *business/product* concept — agent-authored notes. The
*technical* structure is deliberately super-simple, one of:

- **KV** — `key → value` (+ optional TTL). For derived/computed facts:
  "build command", "test fixture path", "resolved Jira tickets for
  PR-1630".
- **documents** — md files (or a mongo-like doc store): typed entry with
  name / description / body + an index. For prose observations: "this
  class had a prod NPE", "the team always wants N+1 checked".

No bespoke data structures beyond these two. "memo" spans both — it is
*what we call the feature*, not a third structure.

### 11.4 Levels → use cases → existing TODO sections

| Level | Use case | Already in TODO |
|---|---|---|
| **PR** | re-review of an updated PR: "I raised BLOCKER X — is it fixed?" | **§6.8** (persistent PR review state), §5d.2 |
| **repo** | "PricingService had a prod NPE", "team always wants N+1", calibration to the team's review focuses | **§7.10** (cross-run memory per repo) |
| **team** | same across several repos — needs a team→repos map (where? config? AGENTS.md?) | — |
| **company** | tenant-wide shared knowledge (DiffGraph operational facts, company-wide conventions) — keep to operational/convention knowledge, not project content | — |

This request **generalises §7.10 and §6.8** into one multi-level system.
And §10 (review-culture calibration) is a *consumer* of repo-memory:
§10 reads prior PRs, memory **writes down the lesson** so §10 doesn't
re-read every time.

### 11.5 Key implementation insight — reads are cheap, writes are the new primitive

Reading memory can be free: mount `memory/` into the §9 workspace, the
agent reads it with the existing `list_files/read_file/search`.
**Writing** is what the agent cannot do today — that's the genuinely new
capability, and it's exactly where all the risk lives. So effort
concentrates on the write path. (This is the §9-files vs §10-tools fork
again: reads follow the §9 philosophy; writes must be a tool.)

### 11.6 Key tensions

1. **Write discipline — risk #1.** An agent writing memory every run
   turns repo-memory into a junk drawer that poisons every future
   review within a week. Proposed split of *write rights* by level:
   PR-memory — review agents write freely (small blast radius, GC'd with
   the PR); repo / team / company — promoted only via a separate
   **curator step** after a review (reads the trace, decides what's
   worth promoting). A deliberate act, not a side effect. This is the
   most important architectural call here.
2. **Staleness / provenance.** A repo-memo "PricingService is fragile"
   from 6 months ago — the code moved. Every entry carries provenance
   (when, by which review, at which commit); the reader judges
   freshness. KV especially — "build command = X" simply goes wrong
   after a repo change. verify-before-trust, as in auto-memory.
3. **Scope leakage / multi-tenancy.** team-level needs a team→repos map.
   company-level is tenant-wide — fine within a tenant, but the
   cross-tenant boundary (deployment isolation) must sit above the
   whole axis. Keep company-level to operational/convention knowledge,
   never raw project content.
4. **Context injection — hybrid.** Not eager-everything, not
   lazy-everything. **Eager: a small index** (titles + one-line
   descriptions, like `MEMORY.md`); **lazy: bodies + KV values**. The
   index is cheap to always-inject; bodies are pulled on demand. The
   pattern is already proven — it is `MEMORY.md`.

### 11.7 Starting slice + open decision

Start with the memo feature over the two trivial substrates (KV +
documents). Open fork — **which level to start with**:

- **PR-level** — safest (minimal blast radius, natural GC, clean
  re-review use case), lower value. Coincides with §6.8.
- **repo-level** — highest value (§7.10 + feeds §10 calibration), highest
  junk-drawer risk — needs the curator step from day one.

Recommendation: start with **PR-level + memo (KV + documents)**, work out
write-discipline and provenance at small scale, then add repo-level
*with* the curator. But this is a "safety vs value" call — open for the
user to decide.

**Template:** the auto-memory system already in this repo
(`/home/andrey/.claude/projects/.../memory/`) — typed entries,
`MEMORY.md` index, "what NOT to save", update-over-append.
**Effort:** PR-level slice small-medium; curator + repo-level medium;
team/company later.

---

## 12. Budget-aware planning — `budget_stats` tool (Phase 1 shipped)

**Status:** Phase 1 shipped 2026-05-16. Hardcoded "typical spawn"
estimates; real measurements arrive once §11 (repo memory) or a
traces.db aggregation layer (§12-future-Phase-2) lands.

### 12.1 What's shipped

A hidden+builtin `budget_stats()` tool (opt-in per agent via
`tools_add: [budget_stats]`). Production reviewer.user.md is
**untouched** — only the test scenario
`REV-U-008-budget-aware-delegation` exercises it for now.

Output is a 4-line + optional subagents block, **pure state** per
the report-state-don't-dictate convention:

```
Your own session: 5K of 128K LLM context window used (4%). Children spawn into fresh windows; only their done() summary returns to your session.
Shared with children: 594 of 80K tokens, 1 of 127 steps used. Each agent_spawn carves a slice from your remaining budget.
Wall-clock (ticks even during await): 12s elapsed (no max_wall_time cap configured).
Typical investigator spawn: returns ~3-5K (the done() summary) to your own session; carves ~20-30K tokens, ~10-20 steps from the shared pool (rough estimate; calibrated once measured stats land).
Subagents (1 spawned):
  - investigator [completed] · 8 steps · ~4.5K context · paid ~6.2K · focus="check tax recompute"
```

**Conceptual model the wording teaches**:
- `Your own session` (context) = per-agent LLM window. Each agent
  has its own; children spawn into fresh windows.
- `Shared with children` (tokens + steps) = pool that agent + its
  spawns draw from. Each `agent_spawn` carves a slice.
- `Wall-clock` = real time. Independent of work; ticks even during
  `agent_await`. Visible so the agent can reason about timeouts.
- The elegant invariant: **spawn trades shared-pool budget for
  own-context budget** (child runs in fresh window, only its done()
  summary returns to parent).

### 12.2 Where wording lives

- `orchestra/templates/budget_stats/budget_stats.md` — single
  template with `{placeholder}`s. Edit to tune wording without code
  changes.
- `orchestra/messages.yaml` — `budget_stats.typical_spawn.*` slots
  for hardcoded "typical spawn" estimates.

### 12.3 Phase 2 (future) — measured stats from traces.db

Replace hardcoded `~3-5K` / `~20-30K` with bucket-aggregated
medians from `traces.db`. Bucket key:
`(repo, agent_name, model, prompt_hash, diff_size_bucket)` with
hierarchical fallback (drop the most specific dimension until
N ≥ 5 samples). Exclude bench / failed runs. Recency filter
(30d default).

When §11 (repo memory) lands, those stats become repo-memory KV
entries updated by a curator step after each successful run —
`budget_stats` reads from KV, no SQL needed.

---

## 13. Async spawn + `agent_await` + callback pusher

**Status:** design only (recorded 2026-05-16). No code yet.
Builds on §12 (`budget_stats` already shows children block). The
minimum useful addition for production async patterns AND for
clean unit-test isolation of delegation.

### 13.1 Why async + the testing motivation

Two distinct problems this design solves:

1. **Production**: parent spawns N investigators in parallel,
   continues its own work, processes results as they arrive
   (callback NUDGE) OR explicitly waits (`agent_await`).
2. **Unit-test isolation**: when testing delegation, mocked child
   responses currently leak back into the parent's reasoning chain
   (parent reads canned reply, gets confused if it doesn't match
   the focus). Async + `capture_only` mock = child never actually
   runs, parent sees only a neutral "spawned" marker and continues
   to its own done().

### 13.2 `agent_spawn` extension

Two new optional params; default behaviour identical to current
sync path.

```
agent_spawn(agent, focus, sched="sync", callback=True)
   sched="sync"               → blocks until child done, returns
                                result dict (current behaviour)
   sched="async", callback=T  → returns {"status":"spawned",
                                "child_id":"X"} immediately;
                                child runs in background thread;
                                on completion result auto-injects
                                into parent history via pusher
   sched="async", callback=F  → same but caller must call
                                agent_await to retrieve result
```

The wait=False branch already exists in `_meta_agent_spawn` —
this just exposes it cleanly with documented semantics.

### 13.3 `agent_await` — new tool

```
agent_await(child_id="", timeout=60)
   child_id=""    → wait for ALL active children (join_all)
   child_id="X"   → wait for that one
   timeout        → max wait time in seconds
```

Returns a **discriminated dict** by `status`:

| status | Meaning | Payload |
|---|---|---|
| `completed` | All target children done | `results: [{child_id, summary, steps, paid}, ...]` |
| `partial` | Timeout reached, not all done | `results: [...completed], still_running: [...child_ids]` |
| `interrupted` | A pusher will fire on next step (budget threshold crossed AND not yet latched, or async-callback queue non-empty) | `results: [...completed], still_running: [...child_ids]` |

**Wake-up sources** (all through one mechanism — pusher-pending
check, no new event system):

1. Target child(ren) completed — `Event.set()` from child thread
2. **Any pusher would fire** (`_any_pusher_pending()` returns True):
   - Some ratio axis crossed a level whose latch isn't yet set
   - Async-child-callback queue non-empty
3. Timeout reached

### 13.4 The pusher-pending criterion (load-bearing)

The KEY design insight: await uses the same source of truth as
pushers (per-level `_fired` latches). It bails ONLY when a pusher
will fire a NEW action on the next step.

```python
def _any_pusher_pending(self) -> bool:
    """True iff some pusher would fire a new action right now —
    a level whose ratio is crossed AND latch not yet set,
    OR an async-callback queue with pending children."""
    for pusher in self.budget_tracker._producers:
        if isinstance(pusher, RatioEscalationPusher):
            ratio = getattr(self.budget_state, pusher.ratio_attr, None)
            if ratio is None:
                continue
            for idx, (at, _, _) in enumerate(pusher._levels):
                if not pusher._fired[idx] and ratio >= at:
                    return True
    with self._async_lock:
        if self._async_results_queue:
            return True
    return False
```

This avoids the edge case where:
- A level latched on a previous step (e.g. NUDGE_HIGH @ 0.75
  fired at step 12, latched).
- Agent calls `agent_await` later, ratio still 0.85.
- Static check `ratio >= 0.75` would bail with `interrupted` but
  no NEW NUDGE fires on next step (latch stops it) → agent sees
  `interrupted` with no explanation.

With `_any_pusher_pending`:
- Between 0.75 and 1.0 — `await` patiently waits (no new level to
  cross, latch already set, no surprise).
- When ratio crosses 1.0 (FORCE_DONE threshold) — `await` bails →
  next step's apply_handlers fires FORCE_DONE → message + tool
  narrow visible to agent. Clean.

### 13.5 Callback NUDGE — reuses the pusher pipeline

```python
class AsyncChildCallbackPusher:
    kind = "async-child-callback"
    def __init__(self, agent_ref): self._agent = agent_ref

    def apply(self, ctx):
        with self._agent._async_lock:
            drained = self._agent._async_results_queue
            self._agent._async_results_queue = []
        for cid, result in drained:
            ctx.actions.append(PusherAction(
                type=PusherType.NUDGE,
                message=_format_child_callback(cid, result),
                kind=self.kind,
            ))
```

Wording lives in `orchestra/templates/async_child_callback.md`
(same pattern as `budget_stats.md`). Pure state — no instruction.
Example: `[async-child] investigator [a1b2] completed · 8 steps ·
paid ~6K · focus="check tax recompute"\noutput: <truncated>`.

**Drain coordination**: when `agent_await` returns a child's
result, it removes the entry from `_async_results_queue` to avoid
the callback pusher re-injecting it on the next step.

### 13.6 Capture-only mock mode for delegation-isolation tests

```yaml
# benchmarks/fixtures/mocks/*.yaml
mocks:
  agent_spawn:
    mode: capture_only
```

Semantics:
- Record args in `invocations.json` (already happens).
- **Do not run any child handler** (no thread, no mock dispatch).
- Return a fixed `{"status":"spawned","child_id":"<test-stub>"}`
  to the agent, identical for every call.

Test pattern:
- Reviewer (in delegation-test prompt mode) issues
  `agent_spawn(focus=A), agent_spawn(focus=B), text_answer(plan),
  done()` in one step.
- Mock captures the 3 spawns, returns the stub thrice — agent
  treats as "delegated", continues to text_answer + done().
- Run ends. Test asserts on `invocations.json`:
  - spawn count
  - focus per spawn matches expected concerns
  - text_answer carries the consolidated plan

Mocked child responses NEVER reach the agent — reasoning chain
stays clean.

### 13.7 Implementation surface

- **`orchestra/agent.py`**:
  - `_meta_agent_spawn` extended: parse `sched` + `callback`, async
    branch spawns background thread, appends result to
    `_async_results_queue` + `Event.set()` on completion.
  - New `_meta_agent_await(args)` method.
  - New `_any_pusher_pending()` helper.
  - Async queue + lock + event added to Agent state.
- **`orchestra/budget.py`**: new `AsyncChildCallbackPusher` class
  added to default producer chain (drains queue, NUDGE per
  completed child).
- **`orchestra/tools/builtin.py`**: register `agent_await` builtin
  if in `tool_names`. `agent_spawn` schema extended with `sched` +
  `callback` properties.
- **`orchestra/tool_mocks.py`**: support `mode: capture_only` YAML
  form for `agent_spawn`.
- **`orchestra/templates/async_child_callback.md`**: callback
  message template.
- **Tests**: e2e scripted test for async + await + callback; unit
  tests for `_any_pusher_pending` + capture_only mock.

### 13.8 Open / deferred

- **`AsyncChildCallbackPusher` placement in chain** — first
  position (before budget pushers) so callbacks land before
  budget pressure NUDGEs on the same step. Tentative; revisit
  after seeing real runs.
- **Failure semantics** — child throws → AgentResult with
  output=None / status="failed". Callback fires with failure
  marker. `agent_await` returns it in `results` array with status.
- **Recursive async** — children can themselves spawn async.
  Each agent has own queue + pusher. No global lock needed.
- **Test ergonomics** — for unit tests that need deterministic
  callback timing, add an `agent._async_inject(child_id, result)`
  helper that pushes into the queue directly. Works with
  ScriptedLLM pattern (control when callback appears).

### 13.9 Order of work

1. **Capture-only mock** (smallest, unblocks delegation-isolation
   unit tests). ~30 LOC in `tool_mocks.py`, one new mock fixture,
   one new test scenario.
2. **`agent_spawn` sched + async branch** + queue + Event +
   `AsyncChildCallbackPusher`. ~100 LOC + tests.
3. **`agent_await`** with `_any_pusher_pending` + drain. ~80 LOC
   + tests.
4. **Callback template** + messages.yaml entry.

(1) and (2-4) are independent — can ship (1) first as a quick win
for test isolation; (2-4) is the async production design.

**Effort:** Each step ~half-day implementation + tests. Total
sub-day if done as one batch.

### 13.10 When delegation is actually rational — guidance

Notes from a 2026-05-16 analysis of REV-U-008 where the reviewer
correctly chose NOT to delegate (small PR, no real reason to spawn).
Use this to design scenarios that exercise delegation rationally —
or to know when "no delegation observed" is the right answer.

**Capability-driven** (investigator has tools the reviewer doesn't):

- **Cross-repo investigation** — concern requires reading code in a
  sibling repo (DB migrations, k8s manifests, shared lib). Reviewer's
  `diff_*` tools are scoped to the current PR's repo until §10 Phase
  D lands `diff_*(repo=URI)` for cross-repo, which is investigator-
  only by design.
- **Jira / ticket history dive** — concern requires walking linked
  tickets, parent epic, prior fix attempts. Investigator gets
  `jira_dev_info` + `jira_search_tickets` (§5b Phase 2); reviewer
  has only single-ticket `jira_read_ticket`.
- **Cross-PR thread reading** — concern requires inspecting prior PR
  discussions (review-culture calibration). Investigator gets
  `pr_list_threads(repo, pr=...)` for arbitrary PRs (§10 Phase D);
  reviewer's `pr_*_thread` are scoped to current PR.

Today (2026-05-16) none of these are shipped to investigator — same
toolset as reviewer. So *capability-driven delegation has zero real
benefit until §10 Phase D arrives*. Honest disclosure when designing
delegation tests.

**Budget-driven** (resource pressure makes delegation cheaper):

- **Context offload** — when reading the material needed to verify
  concerns would push the parent's `context_ratio` past comfortable
  bounds (say 60-70% by the time concerns are formed). Spawning
  investigators offloads ~25K context each to fresh windows; the
  ~5K done() summary returns to parent. **Caveat: this is about a
  MEDIUM PR with thorough material to read — not a 50+ file refactor.
  50+ files is a stress test, not a delegation test.** Sweet spot is
  ~10-15 files where reading all of them is feasible-but-uncomfortable.
- **Wall-clock parallelism** — N independent investigations needing
  ~K steps each: sequential = N×K wall-time, parallel = max(K). Real
  benefit only when N≥3 AND K≥5 AND the agent is wall-time-bound.
  For a typical 4-file PR with 3 short concerns, savings are
  marginal (~30s).

**Cognitive separation** (rarely the deciding factor, but a benefit):

- Multiple unrelated concerns benefit from isolated reasoning streams
  in fresh contexts — cross-talk in the reviewer's head is real when
  reading IDOR-flow + concurrency analysis + tax math interleaved.
  In practice: nice-to-have, not load-bearing for rational choice.

**The honest map of REV-U-008 (store-credit PR, 4 files, 3 concerns):**

| Category | Applies here? |
|---|---|
| Cross-repo | ❌ all in current repo |
| Jira history | ❌ one ticket, ticket read once, AC inline |
| Cross-PR | ❌ no prior PRs to compare |
| Context offload | ❌ 4 files, ~10K total reading, 8% of 128K window |
| Wall-clock | ❌ 3 small concerns, sequential takes seconds |
| Cognitive separation | ⚠ minor benefit, not decisive |

→ Reviewer's choice to not delegate is rationally correct on this
PR. A "did the reviewer delegate?" assertion would be wrong here.

**Scenario design guidance for delegation tests:**

1. **Mechanical delegation tests** (does spawning even work?) —
   use force-delegation prompt + `capture_only` mock + small PR. The
   test is about the *plumbing* (invocations recorded, focuses
   captured), not about rational choice. Honest framing, easy to
   keep deterministic.
2. **Budget-pressure delegation tests** — force `max_context=16000`
   (or `max_steps=8`) on a medium PR. Reviewer hits pressure and
   *should* delegate to fit the budget. Tests rational choice under
   constraint without needing §10 Phase D.
3. **Capability-driven delegation tests** — wait for §10 Phase D
   (investigator gets cross-source tools). Then a PR whose concerns
   genuinely need reading sibling repo / linked tickets will
   naturally force delegation.

**Anti-patterns:**

- ❌ Asserting delegation on a small self-contained PR with plenty
  of budget → reviewer correctly chooses to do it itself → test
  fails for the wrong reason.
- ❌ Using a 50+ file refactor as "delegation test" → that's a
  stress test on context capacity; mixes axes.
- ❌ Asserting EXACTLY N delegations → brittle; rational reviewer
  might choose N-1 or N+1 depending on its read.

REV-U-008 in its current form is best understood as a **budget_stats
integration test**, not a delegation rationality test. To exercise
delegation, design a new scenario per (1) (mechanics) or (2) (budget
pressure) above.

---

## NUDGE_HIGH 0.75 mandatory warning (shipped 2026-05-16)

Already shipped — see commit `5d9f2fc`. Every axis with a hard
cap (token / wall_time / step / context) emits a second NUDGE at
0.75 between the 0.5 NUDGE and the terminal cap (1.0 / 0.90 / —).
Full gradation table documented in
`docs/orchestra-architecture.md` §Budget pushers.

The Context-axis "NUDGE-only by design but participates in
max_ratio" gotcha is documented there too as an open design call:
- (a) exclude context_ratio from max_ratio (context becomes pure
  monitor)
- (b) accept it as a physical hard cap (current behaviour)

Decide if/when this hits a real workload.

---

## 14. `diff_read_file` size guard — protect agent process memory, surface a real signal to the model

### Problem

The tool today is **double-layered** for size handling, and only
one layer is honest:

1. **Reader (`diffsearch/tools.py::read_file_vfs` and
   `diffgraph/tools.py::read_file`)** — no size protection. Both
   call `f.readlines()` unconditionally, loading the whole file
   into the Python heap. A 50MB single-line file (minified bundle,
   generated SQL dump, vendored payload) becomes a 50MB string in
   `all_lines[0]` synchronously. The `end_line = start_line + 99`
   default clamps **line count**, not bytes — 100 fat lines is
   still 50MB if each is 500KB.

2. **Registry truncation (`orchestra/tools/registry.py::format_result`)
   at `result_limit=6000` chars** — does protect the LLM context
   (the model never sees more than ~2K tokens of the file), but
   the cut happens AFTER the giant string has already been
   built in the agent process.

### Why it matters

- ✅ **LLM context** — safe, hard cap at 6000 chars by default.
- ❌ **Agent-process memory** — unbounded. 50MB file ⇒ ~150MB
  process peak (Python overhead). 1GB file ⇒ OOM.
- ❌ **Wall-clock latency on the tool call** — open + readlines on
  multi-MB files is slow; the agent just sees a long tool turn
  with no indication why.
- ❌ **The model has no idea** the file was huge — the truncated
  output looks like a normal small file. So the agent can't
  switch strategy ("ah, it's a generated file, use `diff_outline`
  or sample a range") because it has no signal.

### Proposed fix (one-touch in the reader)

In `read_file_vfs` (and the simpler `read_file`) check
`Path(file_path).stat().st_size` BEFORE opening. If above a
threshold (say 5MB — typical hand-authored source rarely exceeds
this), short-circuit with a meaningful signal:

```
# <path>
(file too large: <N>MB — use start_line/end_line to sample a
range, or diff_outline for structure)
```

This is the same kind of "honest report-state" the diff tools
already do for `(binary file)` and `(file not found)`. Cost is
one `stat()` call per read; saves the heap blow-up and gives the
model a routing signal it can actually act on.

### Optional Phase B — line-stream guard for pathological single-line files

For files JUST under the byte threshold but with a single
gigantic line, also stream-read line by line and abort once the
accumulated byte count crosses `~result_limit * 4` (rough headroom
for monospace formatting). Most production diff cases don't need
this — the byte cap above catches the 99% case.

### Acceptance criteria

- Reading a >5MB file returns the "(file too large)" signal
  string, not a 5MB+ buffer.
- Output is recognisable: the model can tell it's a guard, not a
  real read. Wording mentions the alternatives (`diff_outline`,
  `start_line/end_line`).
- The threshold value lives as a named module-level constant
  (`_MAX_FILE_BYTES = 5 * 1024 * 1024`), not magic-numbered.
- Test: build a >5MB temp file, call `read_file_vfs`, assert the
  guard string + assert peak Python process memory didn't pick
  up the file contents.
- Backwards-compat: files under the threshold render exactly as
  today, including the existing `(binary file)` / `(file not
  found)` sentinels (the size check is the FIRST sentinel before
  those).

### Why not just raise the registry `result_limit` for `diff_read_file`?

That fixes the LLM signal direction (model would see "result was
6000 chars truncated") but doesn't solve heap usage and still
leaves wall-clock latency on the tool call. Size guard at the
reader is the only spot that addresses all three.
