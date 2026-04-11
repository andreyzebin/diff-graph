# Orchestra -- Technical Specification

## 1. Overview

**Orchestra** is a prompt-defined agent framework (~3,700 LOC). One primitive: the **Agent**. One source of truth: the **Prompt file**.

No pipelines, no state machines, no DAGs. Agents are defined by `.prompt` files with structured `@` headers. The framework provides a runtime (ReAct loop + tools + budget), an LLM compiler that builds an agent registry from prompt files, and observability via SQLite traces. All structure -- coordination, delegation, feedback -- emerges from agent decisions at runtime.

### Control model

| Level | Mechanism | What it changes | Who controls |
|---|---|---|---|
| **Prompt** | system prompt, injected messages | *What* the agent thinks about | `.prompt` file, parent via `adjust_agent` |
| **Params** | temperature, top_p, penalties | *How* the agent thinks | `.prompt` defaults, parent via `adjust_agent` |
| **Model** | model switch | *Who* thinks | `.prompt` default, parent via `adjust_agent` |

All three levels are mutable at runtime by supervisor agents.

---

## 2. Prompt File Format

Single source of truth for an agent. Contains structured `@` headers and a prompt body.

### Format

```
@agent: <name>
@mode: react | single
@capabilities: sgr, spawn, spawn_many, plan, fork, adjust_agent, observe_agents, list_agents
@tools: <comma-separated domain tool names>
@budget: <tokens> tokens, <steps> steps[, <duration>]
@llm: model=<name> temperature=<float> [top_p=<float>] [frequency_penalty=<float>]
@data:
  <field>: <type> -- <description>
@summary: <1-3 sentence description for agent discovery>
---
<prompt body with {placeholder} variables matching @data fields>
```

### Supported `@` headers

| Header | Purpose |
|---|---|
| `@agent` | Agent name (unique identifier in registry) |
| `@mode` | `single` (one LLM call) or `react` (non-deterministic tool loop) |
| `@capabilities` | Which meta-tools the agent gets (see table below) |
| `@tools` | Comma-separated list of domain tool names |
| `@budget` | Token limit, step limit, optional wall time |
| `@llm` | Default LLM parameters: model, temperature, top_p, penalties |
| `@data` | Input schema fields with types and descriptions |
| `@summary` | Short description shown in `list_agents` output |

### `@data` triple duty

Each `@data` field simultaneously serves as:
1. **Input schema** -- what `spawn_agent` must provide
2. **Template variable** -- `{field}` in prompt body is replaced with actual value
3. **Discovery docs** -- shown in `list_agents` output

### `@capabilities` to meta-tools mapping

| Capability | Tools added |
|---|---|
| `sgr` | `reflect` |
| `spawn` | `spawn_agent` |
| `spawn_many` | `spawn_many` |
| `plan` | `plan` |
| `fork` | `fork` |
| `adjust_agent` | `adjust_agent` |
| `observe_agents` | `observe_agents` |
| `list_agents` | `list_agents` |

Every agent always gets `done`.

---

## 3. LLM Compiler

Reads `.prompt` files and builds an **agent registry**.

**Two-pass parsing:**
1. Deterministic -- regex extracts `@` headers. Fast, no LLM cost.
2. LLM fallback -- for prompts without formal headers, an LLM infers capabilities, data requirements, and summary.

**Output:** `AgentRegistry` mapping agent names to metadata (summary, capabilities, input schema, tools, budget, llm_params, prompt template).

**Caching:** by combined file hash. Recompiled only when files change.

**Runtime access:** `list_agents` tool returns the registry. `spawn_agent` validates data against target's schema and injects into `{placeholders}`.

**Data inheritance:** Parent's `data_scope` is auto-injected into child `{placeholders}` when the child's `@data` field matches a key in the parent's scope. The child does not need to explicitly request `"inherit"` — matching fields are injected automatically.

Example: the orchestrator sets `data_scope = {diff_summary, existing_comments, commits}` on the lead agent. When the lead spawns a reviewer, the reviewer's `@data` declares the same fields (`diff_summary`, `existing_comments`, `commits`, `focus`). The matching fields are copied from the lead's scope into the reviewer's prompt `{placeholders}`. The `focus` field comes from the `spawn_agent` call. Zero token waste on re-transmitting shared context.

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

Initial values from `@llm` header. Changed at runtime by:
1. Budget pushers (automated threshold actions)
2. Parent agent via `adjust_agent` tool

Clamped to valid ranges. Every change emits `param_adjusted` event.

### Agent discovery

Agents find each other through the compiled registry via `list_agents`, not by hardcoded names. `spawn_agent` by name after consulting the registry.

---

## 5. Tool System

Everything is a tool. Domain actions, meta-actions, agent control, coordination.

### Domain tools

Registered via Python `@registry.register` decorator or YAML config. Each agent only sees tools in its `@tools` list. Tool results auto-truncated. Multiple tool calls execute in parallel.

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
- Investigate first (get_diff, read_outline), then reflect with what you learned.

### Accountability

Every open question from previous reflect must appear in `resolved_questions` (answered or dropped). No silent omissions.

### Visibility

Readable via `observe_agents`, passable via handoff modes, logged in traces, displayed in CLI live panels.

---

## 8. Budget and Stability

### Budget model

Three dimensions tracked per agent: **tokens**, **steps**, **wall time**.

**Cumulative paid:** budget tracks the sum of per-step deltas with cache discount. Cached tokens are discounted so agents are not penalized for prompt caching.

Agents use their own `.prompt` budget (not parent-allocated). Budget is mutable -- `adjust_agent` can extend or reduce (bounded by `max_feedback_budget_delta`).

### Pushers

Default configuration: 75% nudge + 100% force_done.

| Action | Effect |
|---|---|
| `nudge` | Inject user message (e.g., "budget running low, wrap up") |
| `force_reflect` | Next step: only reflect tool available |
| `force_done` | Next step: only done tool available |
| `custom` | Python hook |

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
- Full execution tree reconstructable from stored events
- Reader API for querying runs, agents, and steps

### Trace server (`trace_server/`)

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

**Runs list** (`/`): initial server render + polling `GET /api/runs` every 3s. New runs flash-highlighted.

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
| Agent config in YAML | Single source of truth is the `.prompt` file | `@` headers compiled to registry |
| Workflow enforcement | Max non-determinism | Methodology encoded in prompt |

---

## 11. Design Principles

| Principle | Implication |
|---|---|
| **Prompt = config** | One `.prompt` file per agent. Headers declare capabilities, body defines behavior. |
| **LLM compiler** | Reads prompt files -> builds agent registry. Deterministic + LLM fallback. |
| **Agent discovery** | Agents find each other by summary via `list_agents`. |
| **Max non-determinism** | The react loop has no predetermined steps. The LLM decides everything. |
| **Tools, not pipelines** | Spawn, fork, adjust -- all tool calls. No topology runner. |
| **Mutable params** | LLM generation parameters are live state. Supervisor agents tune them. |
| **Signals, not actions** | Framework computes behavioral signals but does not act on them. |
| **Budget = only hard constraint** | Pushers are the only forced guardrails. Everything else is soft. |
| **Data flows through `{placeholders}`** | `@data` = input schema = template variables = discovery docs. |
| **Methodology in prompts** | Three-phase review (analyze -> investigate -> judge) is a prompt, not a pipeline. |
| **Cumulative paid with cache discount** | Budget accounting reflects actual cost, not gross token counts. |
| **Crash-safe traces** | SQLite persistence means partial runs are always inspectable. |
