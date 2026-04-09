# Orchestra — Planned Improvements

## Done (implemented)

- ~~3.1 Concerns instead of questions~~ — strategist uses 3-5 concerns, reviewer breaks into sub-questions
- ~~3.2 Question cap (max 5)~~ — in strategist prompt
- ~~3.3 Question IDs in SGR schema~~ — IDs with PUT semantics, fuzzy matching fallback
- ~~3.4 Fuzzy matching in SGR~~ — >50% word overlap matches existing question
- ~~1.5 Child cost in spawn results~~ — spawn returns steps, tokens, sgr_summary
- ~~Budget cache discount~~ — cumulative_paid = sum of per-step deltas, cache_discount=0.1
- ~~Child agents use own .prompt budget~~ — not parent-allocated
- ~~Default pushers from compiler~~ — 75% nudge + 100% force_done for all agents
- ~~SQLite trace DB~~ — events persisted per-step, crash-safe
- ~~HTML trace~~ — split-pane, [⧉] detail tabs, [📋 JSON] copy, Call→Result→Context
- ~~CLI trace command~~ — --log, --list, --run, browser auto-open
- ~~Data inheritance~~ — parent data_scope auto-injected into child {placeholders}
- ~~No handoff context by default~~ — child gets everything via system prompt

---

## 1. Budget Awareness & Cost Control

### 1.1 Budget context injection at start

Inject budget context before strategist's first LLM call:

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

Tool the strategist can call to see remaining budget, children cost, affordable count.

**Where:** `orchestra/tools/builtin.py` + `orchestra/agent.py`.
**Effort:** Small.

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
  strategist  spawning 4 reviewers…  [R1: step 3] [R2: step 5] [R3: step 2] [R4: done]
```

**Where:** `cli.py` — subscribe to child events during spawn_many.
**Effort:** Medium.

---

## 3. Prompt Quality

### 3.1 Budget balance instruction in strategist prompt

```
BUDGET MANAGEMENT:
  Your total budget is shared with all reviewers you spawn.
  Each reviewer costs ~5,000-10,000 tokens.
  Better to spawn 2 thorough reviewers than 4 starved ones.
```

**Where:** `diffgraph/prompts/strategist.prompt`.
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

## 4. Trace Improvements

### 4.1 Trace comparison (diff two runs)

Compare two traces side-by-side:
```
python cli.py trace --diff run1 run2
```

Show: which concerns overlapped, which findings matched, cost difference.

**Where:** `orchestra/trace.py` — new render mode.
**Effort:** Medium.

### 4.2 Trace export to JSON

```
python cli.py trace --run ID --format json > trace.json
```

For programmatic analysis, CI integration, dashboards.

**Where:** `cli.py` trace command + `orchestra/trace_db.py`.
**Effort:** Small.

### 4.3 Trace search

Search across runs by finding title, file, severity:

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
Cost: 12,500 tokens paid (strategist: 4,200 + reviewer×2: 4,150 each)
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

## Priority Order

| # | Item | Impact | Effort | Priority |
|---|---|---|---|---|
| 1.1 | Budget context injection | High | Small | **Do first** |
| 3.1 | Budget balance prompt | High | Small | **Do first** |
| 3.2 | Reviewer efficiency prompt | Medium | Small | **Do first** |
| 3.3 | Diff filtering | Medium | Small | **Do first** |
| 5.1 | Total cost summary | Medium | Small | **Do first** |
| 1.4 | budget_status tool | High | Small | Do second |
| 1.3 | Pre-spawn validation | High | Medium | Do second |
| 2.1 | Agent prefix in trace | Medium | Small | Do second |
| 1.2 | Smart pushers | Medium | Medium | Do third |
| 1.5 | Historical cost tracking | Medium | Medium | Do third |
| 2.2 | Live parallel progress | Medium | Medium | Do third |
| 4.1 | Trace comparison | Low | Medium | Later |
| 4.2 | Trace JSON export | Low | Small | Later |
| 4.3 | Trace search | Low | Small | Later |
| 5.2 | Model comparison | Low | Medium | Later |
