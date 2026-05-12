# Orchestra -- Technical Specification

## 1. Overview

**Orchestra** is a prompt-defined agent framework (~3,700 LOC). One primitive: the **Agent**. One source of truth: the **Prompt file**.

No pipelines, no state machines, no DAGs. Agents are defined by paired `<name>.system.md` / `<name>.user.md` files with YAML frontmatter. The framework provides a runtime (ReAct loop + tools + budget + pusher pipeline), a compiler that builds an agent registry from prompt files, and observability via SQLite traces. All structure — coordination, delegation, feedback — emerges from agent decisions at runtime.

### Control model

| Level | Mechanism | What it changes | Who controls |
|---|---|---|---|
| **Prompt** | system prompt, injected messages | *What* the agent thinks about | `<name>.system.md` + `<name>.user.md`, parent via `adjust_agent` |
| **Params** | temperature, top_p, penalties | *How* the agent thinks | `llm:` frontmatter, parent via `adjust_agent` |
| **Model** | model switch | *Who* thinks | `llm:` frontmatter, parent via `adjust_agent` |

All three levels are mutable at runtime by supervisor agents.

---

## 2. Prompt File Format

Each agent is defined by **two sibling files** with YAML frontmatter (see [README → Prompt architecture](README.md#prompt-architecture--layered-extension-friendly) for the full architecture writeup with examples):

- `<name>.system.md` — stable methodology + base toolkit. The "closed for modification" base layer.
- `<name>.user.md` — per-call task wording + additive frontmatter (`tools_add`, `extra_tools`, `dispatch_mode`). The "open for extension" task layer.

### System-layer frontmatter

Canonical field order (see `diffgraph/prompts/reviewer.system.md` for a live example):

```yaml
---
agent: <name>                       # unique identifier in the registry
mode: react | single                # ReAct tool loop vs one-shot
summary: >                          # shown in list_agents() output
  <1–3 sentences>

tools:                              # full base toolkit, flat list
  - tool_a
  - tool_b

# data: lives in user.md by default — it's the interface contract.
# System.md only carries framework-injected identity fields here
# (e.g. generation / mutation). See "Methodology vs interface split"
# below.
data:
  <framework_field>:
    type: string
    description: "..."

guards:                             # methodology guards only (e.g. text_response).
  text_response:        "<message>" # Interface guards (require_tool:X) live in user.md.

budget:                             # run constraints
  tokens: N
  steps: M
  wall_time: 300                    # optional, seconds; enables TimeBudgetPusher
reflect_interval: K                 # step-cadence reflect nudge; omit to disable

llm:                                # default LLM params (parent can override via adjust_agent)
  temperature: 0.2
  top_p: 1.0
---
<system body with stable methodology — diff view contract, severity rubric, finding shape, …>
```

### User-layer frontmatter

Per-call task layer. Frontmatter is **additive only** — `tools:` (full-replace) is rejected at compile time; use `tools_add` to extend the base toolkit.

```yaml
---
tools_add:                          # additive only; full-replace via `tools:` is rejected
  - list_threads
  - spawn_agent
  - post_comment
extra_tools:                        # optional: capture-style tools registered per-run
  - name: text_answer
    description: "Submit your final text."
    parameters:
      type: object
      properties:
        text:
          type: string
      required:
        - text
dispatch_mode: native | meta        # default native (direct tool calls);
                                    # meta = list_tools/call_tool MCP-style

# Interface contract — the data the agent receives at spawn time
# under THIS invocation surface (Bitbucket-PR comment, CLI, Slack, …).
# Different user.md files reuse the same system base with different
# data schemas.
data:
  pr_title:    {type: string, description: "..."}
  comment_id:  {type: integer, description: "..."}
  commits:     {type: string, from: pr_context.commits}

# Interface-half of the guards block. Methodology guards live in
# system.md. The compiler merges both layers into one guards dict;
# a trigger declared in both layers is a compile error.
guards:
  require_tool:post_comment: "..."
---
<user body — per-call task wording with {placeholder} interpolation>
```

### Methodology vs interface split

The two layers split along a **methodology / interface** seam:

| Layer | Carries | Examples |
|---|---|---|
| `system.md` | **Methodology** — what the agent IS, independent of the invocation surface | `tools:` (base capability), `summary:`, `budget:`, `reflect_interval:`, `llm:`, `guards: text_response`, methodology body |
| `user.md` | **Interface** — how the agent is invoked in a concrete environment | `tools_add:` (interface tools — `post_comment`, `set_review_status`, …), `data:` (spawn-time args from the surface), `guards: require_tool:X` (interface guards), task wording |

Effect: the same `reviewer.system.md` can be reused under multiple `*.user.md` files (production Bitbucket, CLI, test prompts) — each declares its own interface contract without touching the methodology.

The compiler **merges** `data:` and `guards:` from both layers into one `AgentRegistryEntry`. A key that appears in both layers is a hard error so a rename can't silently shadow the other side.

### `data:` triple duty

Each `data:` field simultaneously serves as:
1. **Input schema** — what `spawn_agent` must provide
2. **Template variable** — `{field}` in prompt body is replaced with the actual value
3. **Discovery docs** — shown in `list_agents()` output

### Tool registration

All tools — domain (`diff_read_file`, `post_comment`, …) and framework (`spawn_agent`, `list_agents`, `reflect`, `done`) — live in **one flat list** under `tools:`. No separate `@capabilities` indirection. The presence of `reflect` in `tools` is what enables SGR; the presence of `spawn_agent` is what enables delegation.

Framework tools are registered automatically by `orchestra/tools/builtin.py` based on `tools` ∪ `tools_add`. `done` is always available.

---

## 3. Compiler

Reads `<name>.system.md` + `<name>.user.md` pairs and builds an **agent registry**.

**Parsing:** YAML frontmatter (`---` block at top) is the canonical format. A legacy `@key` flat-syntax parser remains for backwards compatibility with older prompts; new prompts use YAML.

**Output:** `AgentRegistry` mapping agent names to `AgentConfig` (system_prompt, user_prompt, tools, budget, reflect_interval, llm_params, input_schema, guards).

**Caching:** by combined file hash (file provider) or commit SHA (Bitbucket provider). Recompiled only when content changes.

**Resource providers:** `compile_prompts()` accepts plain paths, `file://` URIs, or `bitbucket://` URIs. Prompt source is decoupled from the codebase — different agent versions can load prompts from different locations.

| Provider | URI | Hash (mutation ID) |
|---|---|---|
| File | `diffgraph/prompts` or `file:///path/to/prompts/v2` | md5 of file contents |
| Bitbucket | `bitbucket://server/PROJECT/repo/refs/branch/prompts` | commit SHA from API |

CLI: `--prompts` flag overrides default prompt directory. Enables A/B testing at the prompt level via webhook router — each agent config specifies its own `--prompts` URI.

**Runtime access:** `list_agents` tool returns the registry. `spawn_agent` validates data against target's schema and injects into `{placeholders}`.

**Data inheritance:** Parent's `data_scope` is auto-injected into child `{placeholders}` when the child's `data:` field matches a key in the parent's scope. The child does not need to explicitly request `"inherit"` — matching fields are injected automatically.

Example: the orchestrator sets `data_scope = {diff_summary, existing_comments, commits}` on the lead agent. When the lead spawns a reviewer, the reviewer's `data:` declares the same fields (`diff_summary`, `existing_comments`, `commits`, `focus`). The matching fields are copied from the lead's scope into the reviewer's prompt `{placeholders}`. The `focus` field comes from the `spawn_agent` call. Zero token waste on re-transmitting shared context.

**No handoff context by default:** child agents get everything via their system prompt (with injected data), not from parent conversation history.

---

## 4. Agent Model

Two modes:

- **single** -- one LLM call, no tools. For classification, extraction, summarization, planning.
- **react** -- non-deterministic ReAct loop. LLM decides which tools to call, when to reflect, when to spawn, when to stop. Only hard constraints are budget limits and tool availability.

### Execution model

The agent manages its own children. No external runner.

- `spawn_agent(wait=true)` -- parent blocks, child runs, result returned as tool output
- `spawn_agent(wait=false)` -- child runs in background thread, parent continues
- `spawn_many` -- N children in parallel via ThreadPoolExecutor, merged result returned
- `fork` -- clone self into N branches, each with different focus, results merged

### Mutable LLM params

Every agent has a mutable `llm_params` dict: `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `max_completion_tokens`, `model`.

Initial values from `llm:` frontmatter. Changed at runtime by:
1. Pusher pipeline (automated threshold actions — see `orchestra/budget.py`)
2. Parent agent via `adjust_agent` tool

Clamped to valid ranges. Every change emits `param_adjusted` event.

### Agent discovery

Agents find each other through the compiled registry via `list_agents`, not by hardcoded names. `spawn_agent` by name after consulting the registry.

---

## 5. Tool System

Everything is a tool. Domain actions, meta-actions, agent control, coordination.

### Domain tools

Registered via Python `registry.register(...)` or YAML config. Each agent only sees tools listed in its base `tools:` plus any user-layer `tools_add:` extensions. Tool results auto-truncated. Multiple tool calls execute in parallel.

### Meta-tools (9 total)

| Tool | Description |
|---|---|
| `done` | Submit output, stop the loop |
| `reflect` | SGR self-reflection with question ID tracking |
| `spawn_agent(agent, data, focus, context_handoff, wait)` | Create child agent. Data injected into `{placeholders}`. `"inherit"` copies from parent. |
| `spawn_many(agents[], context_handoff, merge)` | Fan-out N agents in parallel. Merge: `union`, `best_confidence`, `llm_merge`, `raw`. |
| `plan(goal, constraints)` | Single-shot planner returning structured JSON |
| `fork(branches[], context_handoff, merge)` | Clone self into N parallel branches |
| `adjust_agent(agent_id, temperature, penalties, model, inject_message, extend_budget_steps)` | Modify running child's LLM params, inject context, adjust budget |
| `observe_agents()` | Status of all children: step, budget, SGR, behavioral signals |
| `list_agents()` | Agent registry: names, summaries, capabilities, input schemas |

### Shared tools (swarm)

| Type | Semantics |
|---|---|
| `append_log` | Append entries, read all. Thread-safe. |
| `mutex_map` | Claim/release keys. Prevents duplicate work. |
| `blackboard` | Key-value store. Last-write-wins. |

---

## 6. Context Handoff

When spawning or forking, the calling agent can choose what context to pass. By default, no handoff context is provided — child agents get everything via `{placeholder}` injection in their system prompt. The lead is instructed not to pass SGR to reviewers (it adds noise — reviewers have their own concerns).

| Mode | What is transferred |
|---|---|
| `full_history` | Complete message list (fork semantics) |
| `sgr_outcomes` | Last reflect() data only |
| `all_sgr` | All reflect() calls (reasoning trajectory) |
| `findings_only` | Only done() output |
| `findings_and_sgr` | done() output + all SGR |
| `condensed` | LLM-summarized history |
| `last_N` | Last N messages with optional SGR bookends |

Modes are composable. Choice is per-call.

---

## 7. SGR (Self-Guided Reasoning)

Structured self-reflection. Backbone of inter-agent communication.

### Schema

`learned` (facts with evidence), `questions_remaining` (things you don't know yet — not things you can already answer), `resolved_questions` (from previous reflect, with concrete answers), `confidence`, `next_action`. Extensible with custom fields.

### Question IDs

Each question gets a stable ID (Q1, Q2...). PUT semantics: same ID across reflects updates text without resetting age. Fuzzy matching (>50% word overlap) links questions even when wording drifts.

### Rules

- `questions_remaining`: only things you genuinely need to investigate. Don't list questions you can already answer — put those in `learned`.
- `resolved_questions`: from PREVIOUS reflect only. Don't open and resolve in the same reflect.
- Don't reflect twice in a row without tool calls between them.
- Investigate first (read_file, read_outline), then reflect with what you learned.

### Accountability

Every open question from previous reflect must appear in `resolved_questions` (answered or dropped). No silent omissions.

### Visibility

Readable via `observe_agents`, passable via handoff modes, logged in traces, displayed in CLI live panels.

---

## 8. Budget and Stability

### Budget model

Three dimensions tracked per agent: **tokens**, **steps**, **wall time**.

**Cumulative paid:** budget tracks the sum of per-step deltas with cache discount. Cached tokens are discounted so agents are not penalized for prompt caching.

Agents use their own per-prompt budget (declared in `budget:` frontmatter, not parent-allocated). Budget is mutable — `adjust_agent` can extend or reduce (bounded by `max_feedback_budget_delta`).

### Pusher pipeline

Per-step middleware chain (`orchestra/budget.py`) that nudges the agent toward progress. One `StepContext` flows through every handler in two phases:

- **Phase 1 — `apply(ctx)`** runs *before* the LLM call. Producers emit `PusherAction`s; consumers translate them into `messages` / `current_tools` mutations + telemetry.
- **Phase 2 — `on_step_done(ctx)`** runs *after* tool dispatch. Stateful handlers inspect `ctx.step_outcomes` (the `(tool_name, is_error)` pair for each call that ran) and update internal state for the next step.

| Handler | Source signal | Behavior |
|---|---|---|
| `ReflectCadenceCounter` | `ctx.step_outcomes` | Owns `steps_since_reflect`. Resets to 0 on a successful `reflect` (validation passed → handler ran); increments per non-reflect non-done tool. Writes the counter into `ctx.steps_since_reflect` in phase 1 so downstream cadence readers see a consistent snapshot. |
| `RatioPusher` | `state.max_ratio` (max of token / step / wall) | User-configurable thresholds from `BudgetConfig.pushers`. Empty by default — per-dimension pushers below cover the default escalation. |
| `TokenBudgetPusher` | `state.token_ratio` | Always-on. 0.5 → NUDGE, 0.75 → FORCE_REFLECT, 1.0 → FORCE_DONE. Messages mention "token budget" so the model can tell which axis is pressing. |
| `TimeBudgetPusher` | `state.wall_ratio` | Same shape as token, no-op without `max_wall_time`. Messages mention "wall-clock budget". |
| `ReflectCadencePusher` | `ctx.steps_since_reflect` | Enabled by `reflect_interval: N` — NUDGE at N steps without reflect, FORCE_REFLECT at 2N, re-arms each reflect cycle. |
| `ApplyActionsHandler` | `ctx.actions` | Applies each pending action: NUDGE appends a user message; FORCE_REFLECT / FORCE_DONE narrows `current_tools`. |
| `TracingHandler` | `ctx.actions` | Emits `BUDGET_THRESHOLD_HIT` per action, tagged with the producer's `kind`. |

| Action | Effect (applied by `ApplyActionsHandler`) |
|---|---|
| `nudge` | Append a user-role message to the conversation. |
| `force_reflect` | Narrow `current_tools` to `reflect` only for the next LLM call. |
| `force_done` | Narrow `current_tools` to `done` only for the next LLM call. |
| `custom` | Call a custom Python hook with `(messages, state)`. |

`reflect` itself flows through `registry.dispatch` like every other tool — JSON-Schema validation runs first. Malformed reflect args (e.g. when a model emits broken JSON and the parser salvages only one field) return a `validation error: …` string as the tool_result; the model sees the error in its next LLM turn and self-corrects. The cadence counter only resets on a reflect call where the handler actually executed (validation passed), so a malformed reflect doesn't pretend to satisfy the cadence and the next NUDGE / FORCE_REFLECT still fires on schedule.

Adding a new producer = one class with `kind` + `apply(ctx)` (and optionally `on_step_done(ctx)`), plug into `BudgetTracker._producers`. Apply + trace consumers stay untouched.

### Message condensation

| Strategy | How |
|---|---|
| `llm_summary` | LLM summarizes old messages |
| `sliding_window` | Keep last N messages |
| `drop_tool_results` | Truncate tool results |
| `hybrid` | Drop results first, then summarize |

SGR reflect() calls optionally exempt. System message never condensed.

### Stability guarantees

| Mechanism | Prevents |
|---|---|
| Budget (tokens/steps/wall) | Runaway cost, infinite loops |
| Depth limit | Infinite spawn/fork recursion |
| Budget partitioning | Children consuming unbounded resources |
| Param clamping | Invalid LLM parameter values |
| Budget extension cap | Supervisor inflating child budget |
| Condensation | Context window exhaustion |
| force_done pusher | Agent never terminating |
| Tool result truncation | Single result flooding context |

### Behavioral signals (read-only)

| Signal | What it detects |
|---|---|
| `repetition_score` | Same tools/args in sliding window |
| `progress_score` | Files explored, questions resolved, findings produced |
| `stuck` | High repetition + low progress |

Available via `observe_agents`. Framework does not act on them -- supervisor agents decide.

---

## 9. Trace System

### SQLite trace DB (`trace_db.py`)

Events persisted per-step to a SQLite database. Crash-safe -- partial runs are recoverable.

- Per agent: agent_id, parent_id, per-step tool calls + tokens + LLM params, SGR history, budget consumed, output
- Per run: model, prompt_source (URI/path), prompt_hash (commit SHA or content md5)
- Full execution tree reconstructable from stored events
- Reader API for querying runs, agents, and steps
- Auto-migration: new columns added on connect if missing (existing DBs)

### Trace server (`tracing/server/`)

FastAPI + Alpine.js web viewer with Jinja2 templates. Two views accessible via separate routes:

**Navigator** (`/runs/{id}/trace`):
- Split-pane: agent tree left (recursive Jinja2 macros), detail tabs right (draggable divider)
- `[⧉]` buttons fetch full data from API (`/api/runs/{id}/step/{agent_id}/{step}/messages`, `/call`, `/result`)
- Right panel toolbar: `📋 Copy` + `{ } JSON` toggle (plain text vs pretty JSON)
- Content display: message content as plain text, tool call arguments as pretty-printed JSON
- Result delta: API returns only new tool messages per step (not accumulated)
- Token usage per step: `↑` uncached input (`prompt_tokens - cached_tokens`), `↓` completion, `©` cached
- Agent header totals: `↑total_in ↓total_out ©total_cached`
- Step summaries show tool name + first argument preview (e.g. `read_file(OrderService.java)`)

**Live** (`/runs/{id}/live`):
- Real-time event stream via WebSocket (`/ws/live/{run_id}?after=N`)
- Bulk-loads existing events via `GET /api/runs/{id}/events` on open, then WebSocket for incremental updates
- Child agents color-coded with `[reviewer:Focus]` tags (6 rotating colors)
- Tool args preview, token usage (`↑↓©`) in event lines
- Auto-scroll pauses when user scrolls up, resumes at bottom

**Runs list** (`/`): initial server render + polling `GET /api/runs` every 3s. New runs flash-highlighted. Prompts column shows generation name + mutation hash (e.g. `main (a1b2c3d)`, `v2 (b7c)`).

Both views link to each other. `/runs/{id}` redirects to `/live` if running, `/trace` if completed.

**Templates:** `macros.html` (recursive agent tree, LLM call steps, SGR entries, findings), `trace.html` (navigator layout), `runs.html` (run list), `live.html` (live view)

### CLI trace commands

- `cli.py trace` -- open last run in browser (starts trace server)
- `cli.py trace --log` -- console trace (call -> result per step, agent tree)
- `cli.py trace --list` -- recent runs table
- `cli.py trace --run ID` -- specific run

### Events

| Event | Key data |
|---|---|
| `agent_started` | agent_id, agent_name, depth |
| `agent_step` | agent_id, agent_name, step, tool, args, tokens |
| `agent_reflect` | agent_id, agent_name, learned, confidence, questions |
| `agent_done` | agent_id, agent_name, output, tokens |
| `agent_forced_done` | agent_id, agent_name, reason |
| `agent_spawned` | parent_id, child_id, agent_name, focus |
| `agent_stream` | agent_id, agent_name, tool_name, args_preview |
| `agent_tool_result` | agent_id, agent_name, tool, result_len |
| `param_adjusted` | agent_id, param, old_value, new_value, source |
| `budget_threshold_hit` | agent_id, threshold, ratio |
| `stuck_detected` | agent_id, repetition_score, progress_score |
| `condensation_triggered` | agent_id, strategy |

---

## 10. What the Framework Does NOT Do

| Deliberately absent | Why | Alternative |
|---|---|---|
| Predefined topologies / DAGs | Structure should emerge from reasoning | Agents spawn at runtime via tools |
| Parameter schedules / curves | Intelligence lives in prompts | Supervisor with `adjust_agent` |
| Declarative feedback loops | Same | Supervisor with `observe_agents` + `adjust_agent` |
| Separate agent-config YAML | Single source of truth is the prompt-pair (`.system.md` + `.user.md`) | YAML frontmatter compiled to registry |
| Workflow enforcement | Max non-determinism | Methodology encoded in prompt |

---

## 11. Design Principles

| Principle | Implication |
|---|---|
| **Prompt = config** | Two sibling files per agent (`<name>.system.md` + `<name>.user.md`). Frontmatter declares capabilities; body defines behavior. |
| **LLM compiler** | Reads prompt files -> builds agent registry. Deterministic + LLM fallback. |
| **Agent discovery** | Agents find each other by summary via `list_agents`. |
| **Max non-determinism** | The react loop has no predetermined steps. The LLM decides everything. |
| **Tools, not pipelines** | Spawn, fork, adjust -- all tool calls. No topology runner. |
| **Mutable params** | LLM generation parameters are live state. Supervisor agents tune them. |
| **Signals, not actions** | Framework computes behavioral signals but does not act on them. |
| **Budget = only hard constraint** | Pushers are the only forced guardrails. Everything else is soft. |
| **Data flows through `{placeholders}`** | `data:` = input schema = template variables = discovery docs. |
| **Methodology in prompts** | Three-phase review (analyze -> investigate -> judge) is a prompt, not a pipeline. |
| **Cumulative paid with cache discount** | Budget accounting reflects actual cost, not gross token counts. |
| **Crash-safe traces** | SQLite persistence means partial runs are always inspectable. |
