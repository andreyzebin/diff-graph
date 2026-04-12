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
| 1.5 | Historical cost tracking | Medium | Medium | Do third |
| 2.2 | Live parallel progress | Medium | Medium | Do third |
| 4.1 | ~~Trace web server Phase 1 (basic server)~~ | ~~Done~~ | | |
| 4.1 | ~~Trace web server Phase 2 (live WebSocket)~~ | ~~Done~~ | | |
| 4.1 | ~~Trace web server Phase 3 (Alpine.js)~~ | ~~Done~~ | | |
| 4.1 | Trace web server Phase 4 (comparison + search) | Medium | Medium | Do third |
| 4.2 | Trace JSON export | Low | Small | Later |
| 4.3 | Trace search CLI | Low | Small | Later |
| 5.2 | Model comparison | Low | Medium | Later |
| 7.1 | Bridge: trace DB ↔ pr-analytics | **Critical** | Small-Medium | **Do first** |
| 7.2 | Benchmark gate for mutations | High | Medium | **Do first** |
| 7.3 | Structured mutation axes | High | Small | Do second |
| 7.4 | Mutation generator | High | Medium | Do second |
| 7.5 | Evolution dashboard (3 data sources) | High | Medium-Large | Do second |
| 7.6 | Safety guardrails + auto-rollback | High | Medium | Do third |
| 7.7 | Cross-run memory | Medium | Medium | Later |
| 7.8 | Cost budget + early stopping | Medium | Small | Later |

---

## 7. Evolutionary Prompt Development

Self-sustaining cycle: generate prompt mutations → evaluate → select winners → promote.

### Existing tools

| Tool | What it provides | Repo |
|---|---|---|
| **DiffGraph trace DB** | Per-run: prompt_hash, model, tokens, findings_count, steps | diff-graph |
| **DiffGraph webhook** | A/B routing with sample%, forward/command modes | diff-graph |
| **DiffGraph resource providers** | Load prompts from file:// or bitbucket:// URIs | diff-graph |
| **pr-analytics** | Bitbucket cache (PR, comments, reactions) in SQLite | pr-analytics |
| **pr-analytics analyze-feedback** | LLM-judge: acceptance verdict (yes/no/unclear) per comment | pr-analytics |
| **pr-analytics semantic_acceptance_rate** | % accepted findings (with feedback) | pr-analytics |
| **pr-analytics agent funnel** | agent_comments → feedback_rate → acceptance_rate | pr-analytics |
| **code-review-benchmarks** | Scenarios with expected findings, LLM-judge scoring | code-review-benchmarks |
| **code-review-benchmarks A/B** | `ab --agent-a --agent-b`, compare reports | code-review-benchmarks |

### 7.1 Bridge: trace DB ↔ pr-analytics (critical first step)

Link prompt_hash to business outcomes. Currently trace DB and pr-analytics are separate.

**Option A: enrich pr-analytics with prompt_hash**
- Agent posts comment with metadata tag: `<!-- prompt:a1b2c3d -->` (invisible in rendered comment)
- pr-analytics extracts tag → joins comment outcomes with prompt generation
- Aggregate: per prompt_hash, what is acceptance_rate?

**Option B: enrich trace DB with feedback**
- After run: poll Bitbucket for comment status (resolved/ignored/reactions)
- Write feedback into trace DB alongside run
- Dashboard queries trace DB only

Option A is simpler — pr-analytics already caches comments and has the analysis pipeline. Just need the metadata tag in posted comments and extraction in pr-analytics.

**Effort:** Small-Medium.
**Priority:** **Do first.**

### 7.2 Metrics

#### Business metrics (from pr-analytics, production PRs)

| Metric | Source | Formula | What it measures |
|---|---|---|---|
| **semantic_acceptance_rate** | pr-analytics analyze-feedback | yes / (yes + no) | Are findings accepted by developers? |
| **semantic_acceptance_rate_all** | pr-analytics | yes / total_comments | Real impact including silent findings |
| **feedback_rate** | pr-analytics | comments_with_reactions / total | Are developers engaging at all? |
| **agent_comments** | pr-analytics | count(root comments by agent) | Volume of output |
| **false_positive_rate** | pr-analytics | no / (yes + no) | How much noise? |
| **time_to_merge_delta** | pr-analytics | median(cycle_time with agent) - median(without) | Does agent speed up or slow down PRs? |

Segmented by: prompt_hash, project, severity, time period.

#### Quality metrics (from code-review-benchmarks, controlled PRs)

| Metric | Source | Formula | What it measures |
|---|---|---|---|
| **benchmark_score** | benchmarks judge | weighted score across scenarios | Overall quality |
| **required_found** | benchmarks | count(expected findings detected) | Coverage of known issues |
| **false_positives** | benchmarks | count(agent comments not in expected) | Noise on known code |
| **regression_count** | benchmarks compare | findings lost vs previous run | What broke? |
| **scenario_pass_rate** | benchmarks | scenarios passed / total | Reliability |

#### Efficiency metrics (from trace DB, any run)

| Metric | Source | Formula | What it measures |
|---|---|---|---|
| **tokens_per_finding** | trace DB | avg(total_tokens_paid / findings_count) | Cost efficiency |
| **steps_per_finding** | trace DB | avg(total_steps / findings_count) | Convergence speed |
| **cache_hit_ratio** | trace DB events | sum(cached_tokens) / sum(prompt_tokens) | Cache optimization |
| **tool_waste_ratio** | trace DB events | redundant tool calls / total calls | Tool usage quality |
| **convergence_steps** | trace DB events | steps to last confidence=high reflect | How fast does agent settle? |

All metrics queryable by prompt_hash → compare generations directly.

### 7.3 Benchmark gate

Every mutation must pass benchmark before touching real PRs.

```
Mutation branch → benchmark runner (CliTrigger with --prompts=bitbucket://...ref/mut-001)
  → SCEN-009: score 0.85 (pass)
  → SCEN-010: score 0.70 (pass)
  → SCEN-011: score 0.90 (pass)
  → Regression: no findings lost vs stable
  → Gate: PASS → enter A/B
```

**Implementation:**
- Script: iterate mutations × scenarios, run benchmark with `--prompts` override
- Compare each mutation vs stable: require score ≥ stable - epsilon
- Reject on regression: any required_found decrease = fail
- Store results in benchmark DB, linked to prompt_hash

**Early stopping:** if 2/3 scenarios fail, skip remaining.
**Cheap screening:** run SCEN-011 (smallest) first.

### 7.4 Structured mutation axes

Each mutation = ONE change along ONE axis with a testable hypothesis.

| Axis | Mutation examples | Primary metric |
|---|---|---|
| **Methodology** | concerns 3→5, one-round→two-round | benchmark_score, acceptance_rate |
| **Tool usage** | changes_only default, search before=2/after=2 | convergence_steps, tool_waste |
| **SGR** | reflect every 3→5 steps, stricter question rules | reasoning quality |
| **Budget** | reviewer 15k→20k, lead 50k→40k | tokens_per_finding |
| **Severity** | stricter BLOCKER criteria, evidence requirements | false_positive_rate |
| **Domain** | Java/Spring patterns, null-safety heuristics | domain-specific acceptance_rate |

Mutations stored as branches:
```
refs/main              — current stable
refs/mut-001-budget    — "reviewer budget 15k→20k"
refs/mut-002-concerns  — "max concerns 5→3"
refs/mut-003-tools     — "recommend search(before=2, after=2)"
```

Commit message = hypothesis. One axis per branch. Combine winners into next stable.

### 7.5 Prompt mutation generator

1. Pick axis + direction from backlog (manual or automated)
2. LLM generates prompt diff with specific change
3. Validate: diff stable vs mutation, confirm only one axis changed
4. Push as branch to prompts repo
5. Run benchmark gate
6. If pass: register in webhook router with sample%

### 7.6 Evolution dashboard

Unified view across all three data sources.

**Per-generation view:**
```
Generation: mut-002-concerns (a1b2c3d)
Hypothesis: "fewer concerns (max 3) → more focused investigation"

Benchmark:
  SCEN-009: 0.85 (stable: 0.82) ✓
  SCEN-010: 0.75 (stable: 0.70) ✓
  SCEN-011: 0.90 (stable: 0.90) =
  Regressions: 0

Production (47 runs, 12 days):
  acceptance_rate: 78% (stable: 71%) ↑
  false_positive_rate: 15% (stable: 22%) ↓
  tokens_per_finding: 4200 (stable: 5100) ↓
  feedback_rate: 45% (stable: 40%) ↑

Verdict: PROMOTE (statistically significant, p<0.05)
```

**Trend view:** generations on X axis, key metrics on Y. See improvement over time.

**Data sources:**
- trace DB: runs table (prompt_hash, tokens, findings) + events
- pr-analytics: comment_analysis (acceptance verdicts), comment_reactions
- benchmarks: scenario scores, regressions

**Implementation:** new page in trace server, queries all three SQLite DBs.

### 7.7 Safety guardrails

| Guard | Trigger | Action |
|---|---|---|
| Benchmark gate | mutation fails any scenario | Block from A/B |
| Rate limit | mutation > N comments on any PR | Reduce sample or pause |
| Regression alert | acceptance_rate drops > 10% vs stable | Auto-pause mutation, notify |
| Auto-rollback | acceptance_rate below absolute threshold | Revert webhook to stable |
| Human approval | before promoting mutation to stable | Manual merge to main |
| Blast radius | initial deployment | sample=5%, increase gradually |

### 7.8 Cross-run memory

Per-repo learned patterns injected into system prompt:

- "This codebase uses @Builder.Default for collections — null checks are likely false positives"
- "Team prefers explicit error handling over @SneakyThrows"
- "Previous runs found 3 @Transactional issues — check for missing annotations"

**Source:** aggregate findings + acceptance from trace DB + pr-analytics by repo.
**Update:** weekly or on N new runs.
**Inject:** new `{learned_patterns}` section in prompts.

### 7.9 Cost budget for evolution

| Phase | Cost control |
|---|---|
| Mutation generation | 1 LLM call per mutation (cheap) |
| Benchmark gate | 3 scenarios × 1 run = 3 runs per mutation |
| A/B production | sample=5% → 1 in 20 PRs. Cap at 50 runs per mutation |
| Feedback wait | No cost (pr-analytics cron) |
| Total budget cap | Configurable per experiment, default 500k tokens |

**Early stopping:** mutation statistically worse after 15 runs → stop, save budget.

### Full evolution cycle

```
┌─────────────────────────────────────────────────────────┐
│ 1. GENERATE                                             │
│    Pick axis → LLM generates prompt mutation             │
│    Push as branch: refs/mut-NNN-axis                     │
│                                                         │
│ 2. BENCHMARK GATE                                       │
│    Run all scenarios with --prompts=bitbucket://mut-NNN  │
│    Compare vs stable. Reject on regression.              │
│                                                         │
│ 3. A/B DEPLOY                                           │
│    Webhook router: add route with sample=5%              │
│    Mutation runs on real PRs alongside stable            │
│                                                         │
│ 4. COLLECT FEEDBACK                                     │
│    pr-analytics caches comments + reactions              │
│    analyze-feedback: LLM-judge acceptance verdicts       │
│    trace DB: link prompt_hash to outcomes via tag        │
│                                                         │
│ 5. EVALUATE                                             │
│    Dashboard: mutation vs stable on all metrics          │
│    Statistical test: is difference significant?          │
│    Business: acceptance_rate, false_positive_rate        │
│    Quality: benchmark_score, regressions                 │
│    Efficiency: tokens_per_finding, convergence           │
│                                                         │
│ 6. DECIDE                                               │
│    Auto: pass thresholds → recommend promote             │
│    Human: review dashboard → approve/reject              │
│                                                         │
│ 7. PROMOTE                                              │
│    Winner → merge to main in prompts repo               │
│    Webhook router: update stable agent config            │
│    New prompt_hash becomes baseline for next cycle       │
│                                                         │
│ 8. REPEAT                                               │
│    Combine winners from different axes                    │
│    Generate next generation of mutations                  │
│    System continuously improves                          │
└─────────────────────────────────────────────────────────┘
```

Traceability at every step: prompt commit SHA → trace DB runs → pr-analytics comments → benchmark scores. Any generation can be reproduced, compared, or rolled back.
