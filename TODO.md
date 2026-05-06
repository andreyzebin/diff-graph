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

## 5c. Pre-deploy QC pipeline (planner / scheduler / metrics / dashboard)

**Goal.** Every new feature/mutation branch gets auto-tested on the
internal LLM infra before merge. No prompt change rolls out without
≥3 successful runs per (provider × scenario) on qwen3-6 + qwen3-coder.
Gentle mode keeps the shared corp endpoints from drowning when the
queue is full.

### Components

1. **Discovery** — poll GitHub for branches matching
   `feature/*` / `mutation/*` (configurable patterns). Diff against a
   tracking file (last-QC'd commit per branch); enqueue only NEW.
   Cron / systemd-timer or long-running watcher.

2. **Planner CLI** (`bench-schedule plan`):
   ```
   bench-schedule plan \
     --branches feature/X,mutation/Y \
     --providers qwen3-6,qwen3-coder \
     --attempts 3 \
     --scenarios all|sentinel|hard \
     --mode aggressive|gentle \
     [--exclude-tag cost:expensive]
   ```
   Emits a queue of `(branch, mutation_hash, provider, scenario, attempt_n)`
   tasks. Sentinel = the 3-5 scenarios known to discriminate prompt
   quality fastest. `hard` = the cost:expensive ones. `all` = full set.

3. **Scheduler** (`bench-schedule run`):
   - aggressive: asyncio.gather + ThreadPool, limited by `--max-concurrency`.
   - gentle: token bucket per provider (configurable in YAML —
     `qwen3-6: 1 req/s, qwen3-coder: 0.5 req/s`). Single-runner-per-bucket.
   - Per task:
     - `git fetch origin <branch>` into a worktree.
     - Run bench scenario, write trace, capture score + warnings + verdict.
     - Push metrics row into pr-analytics QA table.

4. **Metrics storage** in pr-analytics:
   New table `agent_qa_runs`:
   ```
   id PK, branch, mutation_hash, provider, scenario,
   attempt_n, score, verdict, duration_seconds,
   agent_warnings_count, scenario_warnings_count,
   trace_path, started_at, finished_at, status
   ```
   Aggregate view: per (branch, mutation_hash) → pass_rate, median
   score per (provider, scenario), worst-case run.
   Decision rule: a mutation is "deploy-ready" when the worst
   median across required (provider, scenario) pairs is ≥ scenario
   threshold AND attempts ≥ 3.

5. **Dashboard** — extend pr-analytics web:
   - `/qa/branches` — branches under QC + their state (queued / running /
     ready / failed).
   - `/qa/mutations` — per-hash median scores grouped by provider × scenario,
     deploy-ready flag.
   - `/qa/runs` — live + recent runs, status, link to trace.
   - `/qa/runs/{id}` — drill: live log tail (websocket), trace link,
     judge response.
   - `/qa/scheduler` — queue depth, active workers, per-provider rate limit
     status, mode (aggressive / gentle).
   - From a run row, a click drills to its judge response and trace.

6. **Selective coverage** — when queue can't keep up:
   - "Latest N mutations per branch" filter.
   - "Sentinel scenarios only" mode (3-5 scenarios that historically
     discriminate prompts fastest — measured by score variance across
     known good vs known bad prompts).
   - Skip cost:expensive unless explicitly requested.

7. **Branch detection in planner**: file `qa-state.json` per repo
   tracks last-QC'd commit per branch. Discovery diffs git log →
   enqueues. New commit on existing branch supersedes prior runs
   (older runs of same branch stay in history but don't gate deploy).

### MVP slice (smallest useful)

1. Planner CLI + queue file (JSONL).
2. Scheduler that consumes the queue serially in gentle mode.
3. Persist metrics to a new pr-analytics table.
4. CLI report: `bench-schedule report --mutation <hash>` → markdown
   table.

That gives the workflow end-to-end. Dashboard, parallel mode,
discovery, sentinel filtering can come incrementally.

### Where it lives

- Planner + scheduler: `code-review-benchmarks/qa/` (new module),
  CLI entry `bench-schedule`.
- Metrics tables + dashboard routes: `pr-analytics/` (existing
  FastAPI app extended).
- Tracking files (`qa-state.json` etc.): `code-review-benchmarks/qa/state/`,
  gitignored.

### Future (user-driven)

Evolution orchestrator on top of QC: watches outcomes, spawns
mutations along weakest capability axis, cross-breeds dominant
prompts, gradually rolls out (sample %), tracks merge acceptance &
feedback to close the fitness loop. Out of scope for now — the QC
pipeline is the substrate.

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
- Remove `get_diff` tool from agent prompts (replaced by `read_file` with ref range)
- Update prompt instructions: explain ref, vL vs RC, when to use each

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
