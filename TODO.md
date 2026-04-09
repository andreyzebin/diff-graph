# Orchestra — Planned Improvements

## 1. Budget Awareness & Cost Control

### Problem

The strategist spawns N reviewers without knowing if the budget can sustain them. On large diffs, 3 of 4 reviewers hit token limit at 0 useful steps — wasting budget on system prompts and get_diff calls that produce no findings.

### 1.1 Budget context injection at start

Inject a user message before the strategist's first LLM call with computed budget context:

```
BUDGET CONTEXT:
  Total: 50,000 tokens / 50 steps
  Diff size: 26,000 chars / 9 files
  Estimated reviewer cost: ~8,000-12,000 tokens each
  Max affordable reviewers: ~3 (with 5,000 reserved for consolidation)
```

Estimation heuristic:
```python
diff_tokens = min(len(diff_text) / 4, 2000)  # get_diff truncated at 8k
base_cost = 1000 + diff_tokens                 # system prompt + first get_diff
per_step = 400                                  # avg tool call + result
typical_steps = 8
estimated_child_cost = base_cost + per_step * typical_steps

consolidation_reserve = 5000
available = total_budget - consolidation_reserve
max_children = available // estimated_child_cost
```

**Where:** `diffgraph/orchestrator.py` — inject as user message after system prompt.
**Effort:** Small. Pure prompt injection, no framework changes.

### 1.2 Smart pushers (runtime-computed messages)

Replace static pusher messages with callbacks that compute context-aware nudges:

```
at 30%: "Budget 30% used (35,000 remaining). ~3 reviewers affordable. Spawn now if in Phase 2."
at 50%: "Budget 50% used (25,000 remaining). Max ~2 more reviewers. Prepare for Phase 3."
at 75%: "Budget 75% used (12,500 remaining). No more spawning. Consolidate and done()."
at 90%: "Budget 90%. Call done() NOW."
```

The callback has access to `budget_state`, `diff_size`, `children_spawned`, `estimated_child_cost`.

**Where:** `orchestra/budget.py` — pusher callback receives BudgetState. `diffgraph/orchestrator.py` — define callback.
**Effort:** Medium. Needs callback support in pushers (currently message is a static string).

### 1.3 Pre-spawn budget validation

When `spawn_agent` or `spawn_many` is called, the framework:

1. Estimates child cost based on diff size + agent config
2. Validates affordability against remaining budget minus consolidation reserve
3. Adjusts if needed:

```
spawn_many(4 reviewers):
  estimated: 4 × 10,000 = 40,000
  available: 25,000
  → spawns 2 instead of 4
  → result: "Spawned 2/4 reviewers (budget constraint). Remaining tasks merged into reviewer #2's focus."

spawn_agent(reviewer):
  estimated: 10,000
  available: 3,000
  → error: "Insufficient budget for reviewer (need ~10k, have 3k). Consolidate with existing findings."
```

**Where:** `orchestra/agent.py` `_meta_spawn_agent` / `_meta_spawn_many`.
**Effort:** Medium. Estimation logic + spawn reduction + informative error messages.

### 1.4 `budget_status` meta-tool

A tool the strategist can call at any time to see current budget state:

```json
{
  "name": "budget_status",
  "arguments": {}
}
```

Returns:
```json
{
  "total_tokens": 50000,
  "used_tokens": 22000,
  "remaining_tokens": 28000,
  "steps_used": 7,
  "steps_remaining": 43,
  "wall_time_elapsed": "45s",
  "children_spawned": 1,
  "children_total_cost": 8500,
  "estimated_child_cost": 10000,
  "max_affordable_children": 2,
  "consolidation_reserve": 5000,
  "recommendation": "Can afford 2 more reviewers. Reserve 5k for consolidation."
}
```

**Where:** New meta-tool in `orchestra/tools/builtin.py` + handler in `orchestra/agent.py`.
**Effort:** Small. Reads existing BudgetState, adds estimation.

### 1.5 Child cost reporting in spawn results

When a child agent completes, include its actual token consumption in the result:

```json
{
  "status": "completed",
  "agent_id": "abc123",
  "agent_name": "reviewer",
  "output": [...findings...],
  "cost": {
    "tokens_used": 8500,
    "steps_used": 9,
    "wall_time": "12s"
  },
  "sgr_summary": "confidence=high, learned: ..."
}
```

This is already partially implemented (`steps` and `tokens` are returned). Needs formatting into a clear `cost` section.

**Where:** `orchestra/agent.py` `_meta_spawn_agent` return value.
**Effort:** Small. Already have the data, just format it.

### 1.6 Historical cost tracking with complexity tiers

Track cost data across runs, indexed by diff complexity tier. Provides percentile-based estimates instead of flat averages.

**Complexity tiers (map key):**

| Tier | Criteria | Typical scenario |
|---|---|---|
| `tiny` | 1 file, <50 lines | NPE fix, typo, config change |
| `small` | 1-3 files, 50-200 lines | Bug fix, small feature, refactor |
| `medium` | 3-10 files, 200-500 lines | New feature, API change, migration |
| `large` | 10+ files or 500+ lines | Major feature, architectural change |

**Each tier stores percentile distributions:**

```json
{
  "medium": {
    "criteria": "3-10 files, 200-500 lines",
    "samples": 28,
    "cost": {
      "tokens": {"p25": 8000, "p50": 12000, "p75": 18000, "p90": 25000},
      "steps":  {"p25": 12,   "p50": 18,    "p75": 25,    "p90": 35}
    },
    "strategy": {
      "avg_reviewers": 2.3,
      "avg_findings": 4.1,
      "avg_questions_opened": 8.5,
      "avg_questions_resolved": 7.2
    },
    "by_model": {
      "deepseek-chat": {"p50_tokens": 14000, "p75_tokens": 20000, "samples": 20},
      "gpt-4o":        {"p50_tokens": 9000,  "p75_tokens": 13000, "samples": 8}
    },
    "typical": "Java/Spring, 5 files avg, mix new + modified. 2 reviewers: logic + security."
  }
}
```

**Why percentiles not averages:**
- Averages hide bimodality (some reviews finish fast, some hit limits)
- Strategist thinks in risk terms: "at p75 = 18k, I can afford 2 reviewers from 50k with consolidation reserve"
- p90 = worst case for budget validation

**Feeds into budget context injection (1.1):**

```
BUDGET CONTEXT (based on 28 similar medium-complexity reviews):
  Diff: 9 files, ~640 lines → complexity: medium
  Reviewer cost (deepseek-chat): ~14,000 tokens (p50), ~20,000 (p75)
  Recommended: 2 reviewers (at p75, leaves 10k for consolidation)
  Historical: similar reviews found avg 4.1 findings with 2.3 reviewers
```

**Feedback loop — after each run, append data point:**

```json
{
  "timestamp": "2026-04-09T10:22:29",
  "tier": "medium",
  "model": "deepseek-chat",
  "files": 9, "lines": 643,
  "total_tokens": 32500,
  "strategist_tokens": 12000,
  "reviewers": [{"tokens": 8500, "steps": 9}, {"tokens": 6000, "steps": 5}],
  "findings": 6,
  "questions_opened": 15, "questions_resolved": 11
}
```

Percentiles recomputed on append. Different models tracked separately — switching from deepseek-chat to gpt-4o adapts estimates from scratch.

**Tier `typical` summary** generated by LLM from last N data points: "what does a typical medium review look like". The strategist sees: "similar reviews usually need 2 reviewers: one for business logic, one for security."

**Where:** New `orchestra/cost_tracker.py`. Storage: `~/.diffgraph/cost_history.json` or project-local.
**Effort:** Medium. Tier classification, percentile computation, file I/O, integration with estimation.

---

## 2. Parallel Agent Observability

### Problem

When `spawn_many` launches N reviewers in parallel, their events interleave in the actions list:
```
step 0  get_diff        ← reviewer 1
step 0  get_diff        ← reviewer 2  
step 0  get_diff        ← reviewer 3
step 1  read_outline    ← reviewer 1
step 1  find_files      ← reviewer 2
```

No way to tell which reviewer did what.

### 2.1 Agent prefix in parallel actions

When multiple children run concurrently, prefix each action with a short agent identifier:

```
[R1] step 0  get_diff((full))
[R2] step 0  get_diff((full))
[R3] step 0  get_diff((full))
[R1] step 1  read_outline(PricingService.java)
[R2] step 1  find_files(**/*Test*.java)
```

**Where:** `cli.py` — track active children count. If > 1, prefix with agent short id.
**Effort:** Medium. Need to track concurrent children in CLI state.

### 2.2 Separate panels per parallel agent

Instead of interleaving, show separate live panels for each parallel reviewer (stacked):

```
╭── reviewer R1 · step 3/30 ──╮  ╭── reviewer R2 · step 2/30 ──╮
│ search(getItems)             │  │ find_files(**/*Test*)         │
╰──────────────────────────────╯  ╰──────────────────────────────╯
```

**Where:** `cli.py` — Rich Layout with multiple panels.
**Effort:** Large. Requires Rich Layout, tracking N live panels, merging when done.

---

## 3. SGR Quality (Model-Dependent)

### Problem

Deepseek-chat reformulates questions between reflects instead of resolving them: drops 15 old questions, opens 15 new ones that are substantively the same. The SGR accountability rule ("every question must be resolved") is formally satisfied but semantically violated.

### 3.1 Question deduplication in SGR tracker

The SGR tracker detects when a "new" question is semantically similar to a dropped one:

```python
# Simple heuristic: if new question shares >60% words with a dropped question,
# treat it as the same question (don't reset age, don't count as "new")
```

**Where:** `orchestra/sgr.py`.
**Effort:** Medium. Fuzzy matching heuristic, word overlap or embedding similarity.

### 3.2 Prompt reinforcement

Add to strategist prompt:
```
IMPORTANT: Do not reformulate questions between reflects. Keep the same 
question text. If you want to refine a question, resolve the old one 
as "answered" or "dropped" and explain why. Then open the refined version.
```

**Where:** `diffgraph/prompts/strategist.prompt`.
**Effort:** Small. Prompt change only.

---

## 4. Budget-Aware Prompting

### 4.1 Budget balance instruction in strategist prompt

Add to methodology section:
```
BUDGET MANAGEMENT:
  Your total budget is shared with all reviewers you spawn.
  Before spawning, estimate: remaining_budget / estimated_reviewer_cost.
  Always reserve ~20% of total budget for Phase 3 (consolidation + done).
  Better to spawn 2 thorough reviewers than 4 starved ones.
  Call budget_status() if unsure how much budget remains.
```

**Where:** `diffgraph/prompts/strategist.prompt`.
**Effort:** Small. Prompt change.

### 4.2 Budget info in reviewer prompt

Add budget awareness to reviewer:
```
You have a limited token budget. Work efficiently:
- Use read_outline before read_file to target specific lines
- Don't re-read files you've already read
- If budget is running low, focus on your highest-priority finding
```

**Where:** `diffgraph/prompts/reviewer.prompt`.
**Effort:** Small. Prompt change.

---

## 5. CLI Improvements

### 5.1 Total cost summary at end

After findings, show total cost breakdown:
```
Cost: 32,500 tokens (strategist: 12,000 + reviewer×2: 10,250 each)
      18 steps total, 45s wall time
```

**Where:** `cli.py` — accumulate from events, print after findings.
**Effort:** Small. Sum token events.

### 5.2 Progress bar for budget

Show a visual budget bar in the live frame title:
```
╭── strategist · step 7/50 · ████████░░ 65% · ↑32,500 ──╮
```

**Where:** `cli.py` `_render_live_frame`.
**Effort:** Small. Rich progress characters.

---

## Priority Order

| # | Item | Impact | Effort | Priority |
|---|---|---|---|---|
| 1.1 | Budget context injection | High | Small | **Do first** |
| 1.5 | Child cost in spawn results | Medium | Small | **Do first** |
| 4.1 | Budget balance prompt | High | Small | **Do first** |
| 4.2 | Reviewer efficiency prompt | Medium | Small | **Do first** |
| 5.1 | Total cost summary | Medium | Small | **Do first** |
| 1.4 | budget_status tool | High | Small | Do second |
| 1.3 | Pre-spawn validation | High | Medium | Do second |
| 1.2 | Smart pushers | Medium | Medium | Do second |
| 2.1 | Agent prefix in parallel | Medium | Medium | Do third |
| 3.2 | SGR prompt reinforcement | Medium | Small | Do third |
| 1.6 | Historical cost tracking (complexity tiers + percentiles) | Medium | Medium | Do third |
| 3.1 | Question dedup in SGR | Low | Medium | Later |
| 2.2 | Separate parallel panels | Low | Large | Later |
| 5.2 | Progress bar | Low | Small | Later |
