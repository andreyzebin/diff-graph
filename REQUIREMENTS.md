# Orchestra — Prompt-Defined Agent Framework

## 1. Philosophy

One primitive: the **Agent**. One source of truth: the **Prompt file**.

No pipelines, no state machines, no DAGs. Agents are defined entirely by prompt files. The framework provides a runtime (ReAct loop + tools + budget), an LLM compiler that reads prompt files into an agent registry, and observability. All structure — topology, coordination, feedback — emerges from agent decisions at runtime.

### Three levels of control over an agent

| Level | Mechanism | What it changes | Who controls it |
|---|---|---|---|
| **Prompt** | system prompt, injected messages | *What* the agent thinks about | prompt file, parent via `adjust_agent` |
| **Params** | temperature, top_p, penalties | *How* the agent thinks | prompt file defaults, parent via `adjust_agent` |
| **Model** | model switch | *Who* thinks | prompt file default, parent via `adjust_agent` |

All three are mutable at runtime. A supervisor agent can change how a child thinks mid-execution.

---

## 2. Core Concepts

### 2.1 Prompt File = Agent Definition

A prompt file is the single source of truth for an agent. It contains structured metadata headers and the prompt body. No separate YAML config, no Python dataclass — one file per agent.

```
@agent: reviewer
@mode: react
@capabilities: sgr, spawn, plan, fork
@tools: find_files, read_file, read_outline, search, get_diff
@budget: 40000 tokens, 40 steps
@sgr_interval: 3
@llm: model=gpt-4o temperature=0.3
@data:
  diff_summary: string — list of changed files with line counts
  plan: json — review plan from strategist
  existing_comments: string — open PR comment threads
@summary: Performs thorough PR code review. Executes a typed task plan,
  explores the repo with tools, uses SGR to track reasoning, produces
  structured findings with severity and evidence. Can spawn sub-agents
  for deep-dive investigations.
---
You are a senior code reviewer performing a thorough PR review.

WHAT CHANGED:
{diff_summary}

REVIEW PLAN:
{plan}

EXISTING REVIEW COMMENTS:
{existing_comments}

WORKFLOW:
1. Call get_diff() to load the full diff, then read_outline() on changed files.
2. Execute tasks from the plan, highest priority first.
3. reflect() every 3-5 tool calls.
4. If a task needs deep call-chain analysis, spawn() a focused sub-agent.
5. Call done() when all high-priority tasks are complete.
```

#### Prompt file structure

**Header** (above `---`): structured metadata parsed by the compiler.

| Header | Required | Description |
|---|---|---|
| `@agent` | yes | Agent name (unique identifier) |
| `@mode` | no | `react` (default) or `single` |
| `@capabilities` | no | Comma-separated: `sgr`, `spawn`, `spawn_many`, `plan`, `fork`, `adjust_agent`, `observe_agents`, `list_agents` |
| `@tools` | no | Comma-separated domain tool names |
| `@budget` | no | Token limit, step limit, wall time. Default: 40000 tokens, 40 steps |
| `@sgr_interval` | no | Steps between reflection nudges. Default: 3 |
| `@llm` | no | Default LLM params: `model=X temperature=0.3 top_p=1.0 frequency_penalty=0 presence_penalty=0` |
| `@data` | no | Named input parameters with types and descriptions. Become `{placeholders}` in prompt body AND input schema for spawn. |
| `@summary` | yes | 1-3 sentence description. Used by other agents to discover this agent via `list_agents`. |

**Separator**: `---` on its own line.

**Body** (below `---`): the system prompt. Contains `{variable}` placeholders that map to `@data` fields. Injected with actual values when the agent is spawned.

#### `@data` serves three purposes simultaneously

1. **Input schema** — what `spawn_agent` must provide when creating this agent
2. **Template variables** — `{diff_summary}` in the prompt body is replaced with the actual value
3. **Documentation** — `list_agents` shows other agents what data this agent needs

#### `@capabilities` → meta-tools mapping

| Capability | Tools added to agent |
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

### 2.2 LLM Compiler

At startup, the LLM compiler reads all prompt files and builds the **agent registry**.

**Two-pass parsing:**

1. **Deterministic pass** — regex/simple parser extracts `@` headers. Fast, no LLM cost. Handles well-structured prompt files.
2. **LLM fallback** — for prompt files without formal `@` headers (or with incomplete ones), an LLM call extracts: capabilities, data requirements, summary. Handles prompts written in natural language without metadata.

**Output: agent registry** — a structured index of all available agents:

```json
{
  "reviewer": {
    "summary": "Performs thorough PR code review...",
    "capabilities": ["sgr", "spawn", "plan", "fork"],
    "tools": ["find_files", "read_file", "search", "get_diff"],
    "input_schema": {
      "diff_summary": {"type": "string", "description": "list of changed files"},
      "plan": {"type": "json", "description": "review plan from strategist"},
      "existing_comments": {"type": "string", "description": "open PR comment threads"}
    },
    "mode": "react",
    "budget": {"max_tokens": 40000, "max_steps": 40},
    "llm_params": {"model": "gpt-4o", "temperature": 0.3}
  }
}
```

**Caching:** registry is cached by hash of prompt files. Recompiled only when files change.

**Registry is available at runtime:**
- `list_agents` tool returns the registry so agents can discover each other
- `spawn_agent` validates `data` against the target agent's `input_schema`

### 2.3 Agent Modes

Two modes. No others.

**single** — one LLM call, no tools. Input → output. For: classification, extraction, summarization, plan generation, synthesis.

**react** — non-deterministic ReAct loop. The LLM decides at every step which tools to call, when to reflect, when to spawn, when to stop. The framework imposes only budget limits. For: everything else.

### 2.4 Agent Discovery

Agents find each other through the registry, not by hardcoded names.

**`list_agents` tool** returns the compiled registry with summaries, capabilities, and input schemas. The calling agent reads summaries and decides which agent fits its need.

**`spawn_agent` by name** — after consulting `list_agents`, the agent spawns by name with the required data:
```json
spawn_agent(agent="security_auditor", data={diff_text: "...", focus: "auth"})
```

The framework validates `data` against the target's `@data` schema, injects values into `{placeholders}` in the prompt template, and starts the agent.

---

## 3. Tool System

Everything is a tool. Domain actions, meta-actions, agent control, coordination — all tools.

### 3.1 Domain Tools

Registered via Python decorator or YAML. Each agent only sees tools listed in its `@tools`.

```python
@registry.register
def read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    """Read up to 100 lines of a file."""
    ...
```

- Tool results auto-truncated per configurable limit
- Multiple tool calls in one LLM response execute in parallel (`ThreadPoolExecutor`)

### 3.2 Meta-Tools

Added automatically based on `@capabilities`. Agent handles execution internally.

| Tool | Capability | Description |
|---|---|---|
| `done` | *(always)* | Submit output, stop the loop |
| `reflect` | `sgr` | Structured self-reflection (SGR) |
| `spawn_agent` | `spawn` | Create and run a child agent (sync or async) |
| `spawn_many` | `spawn_many` | Fan-out N agents in parallel, return merged results |
| `plan` | `plan` | Spawn a single-shot planner, get structured JSON plan |
| `fork` | `fork` | Clone self into N parallel branches with different focus |
| `adjust_agent` | `adjust_agent` | Change a child's LLM params, inject message, extend budget |
| `observe_agents` | `observe_agents` | Get status of all children (step, budget, SGR, stuck signals) |
| `list_agents` | `list_agents` | Get the agent registry (summaries, capabilities, input schemas) |

#### `spawn_agent(agent, data, context_handoff, wait)`

Creates a child from the registry. `data` fields are injected into the child's prompt `{placeholders}`. Parent's budget is partitioned — child gets a fraction of remaining, parent debited by actual usage.

`wait: true` (default) — parent blocks, gets child output as tool result.
`wait: false` — child runs in background thread. Parent uses `observe_agents` later.

#### `spawn_many(agents[], context_handoff, merge)`

Fan-out + join in one call. Launches N agents in parallel, waits for all, merges results.

Merge strategies: `union` (deduplicate), `best_confidence` (highest SGR confidence), `llm_merge` (spawn a merge agent), `raw` (return all as-is).

#### `plan(goal, constraints, output_hint)`

Single LLM call with a built-in planner prompt. Returns JSON: analysis, tasks, risks, recommendation. Automatically receives parent's current SGR state.

#### `fork(branches[], context_handoff, merge)`

Clones current agent into N copies, each with a different focus. Useful for exploring competing hypotheses. Forks don't get meta-tools (no recursive forking by default).

#### `adjust_agent(agent_id, temperature, penalties, model, inject_message, extend_budget_steps)`

Modifies a running child agent. Changes take effect on the child's next LLM call. This IS the feedback loop — a supervisor agent decides when and how to intervene.

Bounds: temperature [0, 2], penalties [-2, 2], top_p [0, 1]. Budget extension capped at `max_feedback_budget_delta`.

#### `observe_agents()`

Returns all children's status: step, budget ratio, SGR (confidence, questions, learned), behavioral signals (repetition score, stuck flag), last tool called.

### 3.3 Shared Tools (Swarm)

For parallel agents coordinating via shared state.

| Type | Semantics |
|---|---|
| `append_log` | Append entries, read all. Thread-safe. |
| `mutex_map` | Claim/release keys. Prevents duplicate work. |
| `blackboard` | Key-value store. Last-write-wins. |

Created per-execution, injected into agents as regular tools.

---

## 4. Context Handoff

When spawning or forking, the calling agent chooses what context to pass.

| Mode | What is transferred |
|---|---|
| `full_history` | Complete message list (fork semantics) |
| `sgr_outcomes` | Last reflect() data only |
| `all_sgr` | All reflect() calls (reasoning trajectory) |
| `findings_only` | Only done() output |
| `findings_and_sgr` | done() output + all SGR |
| `condensed` | LLM-summarized history |
| `last_N` | Last N messages with optional SGR bookends |

Modes are composable. Choice is per-call, not per-config.

---

## 5. SGR (Self-Guided Reasoning)

The agent's structured self-reflection system. Backbone of inter-agent communication.

**Schema:**
- `learned` — key facts established
- `questions_remaining` — open questions (must be resolved or dropped in next reflect)
- `resolved_questions` — `[{question, resolution: "answered"|"dropped", summary}]`
- `confidence` — `low | medium | high`
- `next_action` — what to do next and why
- Custom extension fields per agent (e.g., `risk_assessment`, `files_analyzed`)

**Accountability rule:** every open question from the previous reflect must appear in `resolved_questions` (answered or dropped). No silent omissions.

**Visibility:** SGR history is readable via `observe_agents`, passable via handoff modes, logged in traces.

---

## 6. Budget, Guards & Stability

The framework's hard constraints. Everything else is soft (prompt-based).

### 6.1 Budget Model

Three dimensions tracked independently per agent:

| Dimension | What it measures | Default |
|---|---|---|
| **Tokens** | Total LLM token consumption (in + out) | 40,000 |
| **Steps** | ReAct loop iterations | 40 |
| **Wall time** | Real clock time | None (unlimited) |

Budget is **mutable** — `adjust_agent` can extend or reduce it at runtime (bounded by `max_feedback_budget_delta`).

When spawning children, budget is partitioned from remaining. Parent debited by child's actual consumption.

### 6.2 Budget Pushers

Configurable `(threshold, action)` pairs. The only framework-imposed behavioral guardrails.

| Action | Effect |
|---|---|
| `nudge` | Inject a user message ("Half budget used. Focus on high-priority tasks.") |
| `force_reflect` | Next step: only `reflect` tool available |
| `force_done` | Next step: only `done` tool available |
| `custom` | Call a Python hook `(messages, budget_state) → None` |

Pushers trigger on whichever budget dimension hits the threshold first.

**Example configuration (in `@budget` or programmatic):**

```
50%  → nudge "Half budget used. Prioritize."
75%  → force_reflect (must reflect before continuing)
100% → force_done (must submit findings now)
```

### 6.3 Message Condensation

Prevents context window exhaustion in long-running agents.

| Strategy | How it works |
|---|---|
| `llm_summary` | LLM call summarizes old messages, replaces them with summary |
| `sliding_window` | Keep last N messages, prepend count of dropped |
| `drop_tool_results` | Keep tool calls but truncate results to N chars |
| `hybrid` | Drop tool results first, then LLM-summarize if still over |

**Preservation rules:**
- System message: never condensed
- SGR reflect() calls: optionally exempt (`preserve_sgr: true`) — they are long-term memory
- Last N messages: always kept verbatim

Condensation is transparent to the agent.

### 6.4 Depth Limit

Prevents infinite spawn/fork recursion.

- Each agent has a `max_depth` (default 3)
- Agents at max depth have meta-tools (spawn, fork, plan) removed from their toolset
- They can still use domain tools, SGR, and done

### 6.5 Behavioral Signals (Read-Only)

The framework computes signals and makes them available. It does NOT act on them.

| Signal | Range | What it detects |
|---|---|---|
| `repetition_score` | 0.0–1.0 | Same tools/args called repeatedly in a sliding window |
| `progress_score` | 0.0–1.0 | Unique files explored, SGR questions resolved, findings produced |
| `stuck` | bool | repetition high AND progress low |

**Available via:**
- `observe_agents` tool (supervisor reads child signals)
- Event bus (external monitoring)

**Who acts on them:** supervisor agents (via prompt), budget pushers (via custom hooks), or external systems (via events). The framework itself does nothing.

### 6.6 LLM Parameters as Mutable State

Every agent has a mutable `llm_params` dict.

| Param | Range | What it controls |
|---|---|---|
| `temperature` | [0, 2] | Exploration vs. exploitation |
| `top_p` | [0, 1] | Nucleus sampling breadth |
| `frequency_penalty` | [-2, 2] | Penalize repeated tokens (break loops) |
| `presence_penalty` | [-2, 2] | Encourage topic diversity |
| `max_completion_tokens` | int | Response length limit |
| `model` | string | Which model generates |

**Who changes params:**
1. Initial values from `@llm` in prompt file
2. Budget pushers (automated threshold actions)
3. Another agent via `adjust_agent` (the primary intelligent control mechanism)

Every change emits `param_adjusted` event with: agent_id, param, old_value, new_value, source.

### 6.7 Stability Guarantees

| Mechanism | What it prevents |
|---|---|
| Budget (tokens/steps/wall) | Runaway cost, infinite loops |
| Depth limit | Infinite spawn/fork recursion |
| Budget partitioning | Children consuming unbounded resources |
| Param clamping | Invalid LLM parameter values |
| Budget extension cap | Supervisor inflating a child's budget without bound |
| Condensation | Context window exhaustion |
| force_done pusher | Agent never terminating |
| Tool result truncation | Single tool result flooding context |

---

## 7. Observability

### 7.1 Event Bus

Every significant action emits a typed event.

| Event | Key data |
|---|---|
| `agent_started` | agent_id, agent_name, parent_id, depth |
| `agent_step` | agent_id, step, tool, args, tokens |
| `agent_reflect` | agent_id, step, learned, confidence, questions |
| `agent_done` | agent_id, output, tokens |
| `agent_forced_done` | agent_id, reason (token/step/wall limit) |
| `agent_spawned` | parent_id, child_id, agent_name |
| `agent_stream` | agent_id, step, tool_name, args_preview, token_count |
| `agent_tool_result` | agent_id, step, tool, result_len |
| `param_adjusted` | agent_id, param, old_value, new_value, source |
| `budget_threshold_hit` | agent_id, threshold, ratio |
| `stuck_detected` | agent_id, repetition_score, progress_score |
| `condensation_triggered` | agent_id, strategy, message_count |

### 7.2 Traces

Every agent produces a structured trace:
- agent_id, parent_id, agent_name
- Per-step: tools called, token usage, LLM params used
- SGR history (all reflect calls)
- Budget consumed
- Output

Full execution tree reconstructable from traces: who spawned whom, what context was handed off, what params were adjusted when and by whom.

---

## 8. What the Framework Does NOT Do

| Anti-pattern | Why not | Alternative |
|---|---|---|
| Predefined topologies / DAGs | Structure should emerge from agent reasoning | Agents spawn/fork at runtime via tools |
| Parameter schedules / curves | Intelligence should live in prompts | Supervisor agent with `adjust_agent` |
| Declarative feedback loops | Same as above | Supervisor agent with `observe_agents` + `adjust_agent` |
| Auto-fork / auto-spawn configs | If you want automatic behavior, encode it in the prompt | Prompt: "if confidence is low after 5 steps, fork" |
| Agent config in YAML | Single source of truth should be the prompt file | `@` headers in prompt file, compiled to registry |
| Persistent state between runs | Stateless execution model | External storage if needed |

---

## 9. Design Principles

| Principle | Implication |
|---|---|
| **Prompt = config** | One file per agent. Headers declare capabilities, body defines behavior. No separate YAML. |
| **LLM compiler** | Reads prompt files → builds agent registry. Deterministic parse + LLM fallback. |
| **Agent discovery** | Agents find each other by summary via `list_agents`, not by hardcoded names. |
| **Max non-determinism** | The react loop has no predetermined steps. The LLM decides everything. |
| **Tools, not pipelines** | Spawn, fork, plan, adjust — all tool calls. No topology runner. |
| **Mutable params** | LLM generation parameters are live state. Supervisor agents tune them. |
| **Signals, not actions** | Framework computes behavioral signals but does not act on them. |
| **Budget = only hard constraint** | Pushers are the only forced guardrails. Everything else is soft. |
| **Data flows through `{placeholders}`** | `@data` declarations = input schema = template variables = `list_agents` docs. |
