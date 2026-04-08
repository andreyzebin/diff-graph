# Orchestra — Agent-First Orchestration Framework

## Philosophy

**One primitive: the Agent.** Everything else — topology, coordination, feedback — emerges from agent decisions via tools and prompts.

No predefined pipelines. No state machines. No declarative DAGs. The framework provides two agent modes (single-shot and react), a tool system, budget constraints, and observability. All structure is created at runtime by agents themselves.

**Three levels of control over agent behavior:**

| Level | Mechanism | What it changes |
|---|---|---|
| **Prompt** | system prompt, injected messages | *What* the agent thinks about — direction, focus, constraints |
| **Params** | temperature, top_p, penalties, max_tokens | *How* the agent thinks — exploration vs. exploitation, diversity, verbosity |
| **Model** | model switch | *Who* thinks — different model with different strengths and biases |

All three levels are controllable by other agents via tools at runtime.

---

## 1. Agent

The only execution unit. Two modes:

- **single** — one LLM call, no tools. For simple tasks: classify, extract, summarize, plan.
- **react** — non-deterministic ReAct loop with tools, optional SGR, budget constraints, and message condensation. The general-purpose mode for everything else.

```yaml
agents:
  researcher:
    system_prompt: prompts/researcher.txt
    mode: react                        # or: single
    sgr: true
    tools: [find_files, read_file, search, get_diff]
    meta_tools: [spawn_agent, spawn_many, plan, fork, adjust_agent, observe_agents]
    output_schema: ReviewFinding[]
    budget:
      max_tokens: 40000
      max_steps: 40
      max_wall_time: 180s
      max_children_budget: 0.3
      pushers:
        - at: 0.5
          type: nudge
          message: "Half budget used. Focus on high-priority tasks."
        - at: 0.75
          type: force_reflect
        - at: 1.0
          type: force_done
    llm_params:
      model: gpt-4o
      temperature: 0.3
    condensation:
      enabled: true
      trigger: 30000
      strategy: llm_summary
      preserve_sgr: true
```

### Requirements

- **R1.1**: Two agent modes: `single` (one LLM call, no tools) and `react` (non-deterministic tool-use loop).
- **R1.2**: Agent config loadable from YAML or Python dicts. Config is data, not code.
- **R1.3**: Each agent has its own independent budget (tokens, steps, wall time).
- **R1.4**: Each agent has mutable `llm_params` (temperature, top_p, frequency_penalty, presence_penalty, max_completion_tokens, model) that can be changed at any point during execution — by the agent itself, by a parent agent via tool, or by budget pushers.
- **R1.5**: Agents are instantiated at runtime from a registered config name. Multiple instances of the same config can run concurrently.
- **R1.6**: An agent's behavior is determined by its **prompt** and **available tools**. The framework imposes no workflow, sequence, or structure beyond the ReAct loop itself.
- **R1.7**: The react loop is maximally non-deterministic: the LLM decides which tools to call, in what order, when to reflect, when to spawn, when to stop. The only hard constraints are budget limits and tool availability.

---

## 2. Tool System

Everything is a tool. Domain actions, meta-actions (spawn, fork, plan), agent control (adjust params, inject messages), coordination (shared state) — all exposed as tools in the agent's toolset.

### 2.1 Tool Registry

```python
@registry.register
def read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    """Read up to 100 lines of a file."""
    ...
```

- **R2.1**: Tools registered via Python decorator or YAML config. Both produce identical internal representations.
- **R2.2**: Each agent's toolset is explicitly configured — not global. An agent only sees tools listed in its config.
- **R2.3**: Tool results auto-truncated per configurable limit.
- **R2.4**: Multiple tool calls in a single LLM response execute in parallel via `ThreadPoolExecutor`.

### 2.2 Meta-Tools (Builtin)

These are the tools that give agents the ability to create structure, coordinate, and control other agents. They are the replacement for predefined topologies, feedback loops, and adaptive schedules.

#### `spawn_agent` — create a child agent

```json
{
  "name": "spawn_agent",
  "arguments": {
    "agent": "security_reviewer",
    "focus": "Check if /api/transfer validates auth tokens",
    "context_handoff": "sgr_outcomes",
    "wait": true
  }
}
```

- **R2.5**: `spawn_agent` creates a child agent from a registered config. The parent specifies: agent config name, focus (injected as user message), context handoff mode, and whether to wait (sync) or continue (async).
- **R2.6**: Synchronous spawn (`wait: true`): parent blocks, child runs to completion, child output returned as tool result. Parent continues its ReAct loop with the new information.
- **R2.7**: Asynchronous spawn (`wait: false`): child runs in parallel. Parent continues. Parent can later call `observe_agents` to check status or collect results.
- **R2.8**: Child's budget is partitioned from parent's remaining budget. Parent is debited by child's actual consumption.
- **R2.9**: Depth limit enforced globally. Agents at max depth have meta-tools removed from their toolset.

#### `spawn_many` — parallel fan-out as a single tool call

```json
{
  "name": "spawn_many",
  "arguments": {
    "agents": [
      {"agent": "security_reviewer", "focus": "auth validation"},
      {"agent": "perf_reviewer", "focus": "N+1 query in loop"},
      {"agent": "logic_reviewer", "focus": "off-by-one in pagination"}
    ],
    "context_handoff": "sgr_outcomes",
    "merge": "union"
  }
}
```

- **R2.10**: `spawn_many` launches N agents in parallel and returns all results as one tool result. This is fan-out + join in a single tool call.
- **R2.11**: Merge strategies for combining results: `union` (concatenate, deduplicate), `best_confidence` (pick highest SGR confidence), `llm_merge` (spawn a merge agent), `raw` (return all results as-is).
- **R2.12**: Budget split across children: `equal` (remaining / N), or proportional to a `priority` field on each agent spec.

#### `plan` — spawn a planner sub-agent

```json
{
  "name": "plan",
  "arguments": {
    "goal": "Break down the security review into concrete investigation steps",
    "constraints": "Focus on the auth middleware changes. Ignore test files.",
    "output_hint": "list of tasks with priorities"
  }
}
```

- **R2.13**: `plan` spawns a lightweight single-shot planner agent that returns structured JSON (analysis, tasks, risks, recommendation). The planner automatically receives the parent's current SGR state (learned facts, open questions) as context.
- **R2.14**: Default planner prompt is built-in but overridable per agent config.
- **R2.15**: Plan result is returned as tool result — the parent agent decides what to do with it. The framework does not execute the plan.

#### `fork` — explore multiple hypotheses in parallel

```json
{
  "name": "fork",
  "arguments": {
    "branches": [
      {"focus": "Assume the race condition IS exploitable. Find evidence."},
      {"focus": "Assume the race condition is benign. Find evidence."}
    ],
    "context_handoff": "full_history",
    "merge": "best_confidence"
  }
}
```

- **R2.16**: `fork` clones the current agent into N parallel copies, each with a different focus. Results are merged and returned as a single tool result.
- **R2.17**: Context at fork point is configurable per call (typically `full_history` for true forking, `sgr_outcomes` for lightweight divergence).
- **R2.18**: Fork depth limit enforced. Forks inherit parent's remaining budget, split equally.

#### `adjust_agent` — control another agent's generation parameters

```json
{
  "name": "adjust_agent",
  "arguments": {
    "agent_id": "abc123",
    "temperature": 0.8,
    "frequency_penalty": 1.5,
    "presence_penalty": 1.0,
    "inject_message": "The auth middleware is bypassed for /api/internal/*. Factor this in.",
    "extend_budget_steps": 5
  }
}
```

- **R2.19**: `adjust_agent` allows a parent/supervisor agent to modify a running child agent's LLM parameters, inject a message into its context, or extend/reduce its budget.
- **R2.20**: Parameter changes take effect on the child's **next** LLM call. They are not retroactive.
- **R2.21**: This is the replacement for static feedback loop configs and adaptive parameter schedules. A supervisor agent with a prompt like *"observe your children, if one is stuck raise its temperature, if one found something important tell the others"* — all logic lives in the prompt, not in YAML.
- **R2.22**: Every `adjust_agent` call emits a `param_adjusted` event for observability.
- **R2.23**: Adjustments are bounded: budget extensions cannot exceed a configurable `max_feedback_budget_delta`. Temperature/penalties are clamped to valid ranges.

#### `observe_agents` — monitor child agents

```json
{
  "name": "observe_agents",
  "arguments": {}
}
```

Returns:
```json
[
  {
    "agent_id": "abc123",
    "agent_name": "security_reviewer",
    "status": "running",
    "step": 12,
    "budget_ratio": 0.45,
    "sgr": {"confidence": "medium", "questions_remaining": 2, "learned": "..."},
    "last_tool": "read_file"
  }
]
```

- **R2.24**: `observe_agents` returns the current state of all child agents spawned by the calling agent. Includes: status, step count, budget usage, last SGR entry, last tool called.
- **R2.25**: This is how a supervisor agent gets the information it needs to decide whether to `adjust_agent`, `inject_message`, or let the child continue.

#### `reflect` — self-guided reasoning (SGR)

- **R2.26**: Available to agents with `sgr: true`. Schema: `learned`, `questions_remaining`, `resolved_questions`, `confidence`, `next_action`, plus custom extension fields.
- **R2.27**: Returns `"Reflection noted."` — no side effects beyond recording. The value is in structuring the agent's own thinking.
- **R2.28**: SGR history is a first-class data structure: extractable for handoff, visible in `observe_agents`, logged in traces.

#### `done` — submit output and stop

- **R2.29**: `done(findings)` submits the agent's structured output and terminates the ReAct loop.
- **R2.30**: Output schema is configurable per agent config.

### 2.3 Shared Tools (Swarm Coordination)

For agents running in parallel that need to coordinate via shared state.

```yaml
shared_tools:
  shared_findings:
    type: append_log
  file_claims:
    type: mutex_map
  notes:
    type: blackboard
```

- **R2.31**: Shared tools are created per-execution and injected into specified agents' toolsets.
- **R2.32**: Types: `append_log` (append + read all, thread-safe), `mutex_map` (claim/release keys), `blackboard` (key-value, last-write-wins), `custom` (user-provided class).
- **R2.33**: Agents interact with shared tools via normal tool calls. The LLM sees them as regular tools.

---

## 3. Context Handoff

When one agent passes context to another (via `spawn_agent`, `spawn_many`, `fork`), the **handoff mode** controls what is transferred. The calling agent chooses the mode per-call — it is not a framework config.

### Built-in modes

| Mode | What is transferred |
|---|---|
| `full_history` | Complete message list (fork semantics) |
| `sgr_outcomes` | Last reflect() structured data only |
| `all_sgr` | All reflect() calls in order (reasoning trajectory) |
| `findings_only` | Only the done() output |
| `findings_and_sgr` | done() output + all SGR calls |
| `condensed` | LLM-generated summary of message history |
| `last_N` | Last N messages, optionally with SGR bookends |
| `custom` | User-provided Python callable |

- **R3.1**: Handoff mode is specified as an argument to spawn/fork tool calls, not as a topology-level config.
- **R3.2**: Built-in modes are composable: `["findings_only", "last_sgr"]` produces a message list combining both.
- **R3.3**: `condensed` mode uses an LLM call to summarize — the condensation prompt is configurable.
- **R3.4**: Custom handoff mode: a Python callable `(messages, sgr_history, output) → messages`.

---

## 4. SGR (Self-Guided Reasoning)

- **R4.1**: Opt-in per agent (`sgr: true`).
- **R4.2**: Schema: `learned`, `questions_remaining`, `resolved_questions` (with `resolution` + `summary`), `confidence` (low/medium/high), `next_action`.
- **R4.3**: Extensible: agents can have custom SGR fields (e.g., `risk_assessment`, `files_analyzed`).
- **R4.4**: SGR history is the backbone of inter-agent communication: it is what gets passed in `sgr_outcomes` handoff, what `observe_agents` returns, what supervisor agents use to decide adjustments.
- **R4.5**: `resolved_questions` enforces accountability — every open question from the previous reflect must move to resolved (answered or dropped). No silent omissions.
- **R4.6**: SGR frequency nudge: configurable `sgr_interval` (default 3 steps). The framework reminds the agent to reflect, but does not force it.

---

## 5. Budget & Lifecycle

### 5.1 Budget Model

- **R5.1**: Budget tracked on three dimensions: tokens, steps, wall time.
- **R5.2**: Pushers are configurable `(threshold, action)` pairs. Actions: `nudge` (inject message), `force_reflect` (restrict to reflect tool), `force_done` (restrict to done tool), `custom` (Python hook).
- **R5.3**: Pushers trigger on whichever dimension hits the threshold first.
- **R5.4**: When spawning children, budget is partitioned from parent's remaining budget. Parent is debited by child's actual consumption.
- **R5.5**: Budget is a mutable object on the agent — `adjust_agent` can extend or reduce it at runtime.

### 5.2 Message Condensation

- **R5.6**: Triggered when message history exceeds a configurable token threshold.
- **R5.7**: Strategies: `llm_summary`, `sliding_window`, `drop_tool_results`, `hybrid`.
- **R5.8**: SGR reflect() calls are optionally exempt from condensation — they are the agent's long-term memory.
- **R5.9**: System message is never condensed.
- **R5.10**: Condensation is transparent to the agent.

---

## 6. LLM Parameters as Mutable State

LLM generation parameters are **mutable state** on every agent, not static config. They can be changed by:

1. **Budget pushers** — automated threshold actions (e.g., force_done at 100%)
2. **The agent itself** — an agent could theoretically request its own param changes
3. **Another agent via `adjust_agent` tool** — the primary mechanism for intelligent, context-aware parameter control

- **R6.1**: Every agent has a mutable `llm_params` dict: `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `max_completion_tokens`, `model`.
- **R6.2**: Initial values come from agent config. Changes are applied immediately to the next LLM call.
- **R6.3**: Every param change emits a `param_adjusted` event with: `agent_id`, `param`, `old_value`, `new_value`, `source` (pusher / self / agent_id of adjuster).
- **R6.4**: Parameters are clamped to valid ranges: temperature [0, 2], penalties [-2, 2], top_p [0, 1].
- **R6.5**: Model can be switched mid-run. Message history format must remain compatible.
- **R6.6**: The framework provides **no** automatic parameter schedules, curves, or drivers. If you want budget-driven temperature decay, write a supervisor agent with a prompt that does it. All intelligence lives in prompts, not in framework config.

### Supervisor pattern (replaces feedback loops and adaptive schedules)

A supervisor is just a react agent with `adjust_agent` and `observe_agents` tools and a prompt like:

```
You are a supervisor overseeing research agents.

Every few steps, call observe_agents() to check their progress.

If an agent's confidence is stuck at "low" after 50% of its budget:
  - Raise its temperature to 0.6-0.8 to encourage creative exploration
  - Inject a message suggesting a different angle

If an agent is repeating the same tools (check last_tool pattern):
  - Set frequency_penalty to 1.5 to break the loop
  - After 2 steps, reduce it back to 0.3

If one agent discovers something relevant to another:
  - Inject the finding into the other agent's context

When all children are done, call done() with the consolidated results.
```

- **R6.7**: The framework provides the tools. The prompt provides the strategy. No YAML-declared feedback loops, no schedule configs, no curve functions.

---

## 7. Behavioral Signals

The framework computes behavioral signals and makes them available to agents (via `observe_agents`) and to budget pushers. These are **read-only observations**, not control mechanisms.

- **R7.1**: **Repetition score** (0.0–1.0): computed from a sliding window of recent tool calls. Configurable: window size, what counts as repetition.
- **R7.2**: **Progress score** (0.0–1.0): computed from SGR question resolution rate, unique files explored, findings produced per step.
- **R7.3**: **Stuck flag**: true when repetition_score > threshold AND progress_score < threshold. Configurable thresholds.
- **R7.4**: Signals are included in `observe_agents` output so supervisor agents can react to them.
- **R7.5**: Signals are included in event bus emissions for observability.
- **R7.6**: The framework does NOT act on signals automatically. No auto-param-adjustment, no auto-nudge from stuck detection. If you want stuck detection to trigger actions, write it in a supervisor's prompt or in a budget pusher.

---

## 8. Configurability

- **R8.1**: Agent definitions are YAML-configurable.
- **R8.2**: Python API for everything YAML can do.
- **R8.3**: Prompt templates support `{variable}` interpolation.
- **R8.4**: Adding a new agent = YAML block + optional prompt file. No code changes.
- **R8.5**: Adding a new tool = Python decorator or YAML registration. No changes to agent loop.
- **R8.6**: Custom budget pushers, handoff modes, and merge strategies via Python hooks.
- **R8.7**: Shared tools configurable in YAML per execution context.

---

## 9. Observability

- **R9.1**: Every agent execution produces a structured trace: agent_id, parent_id, steps, tools called, SGR history, budget consumed, LLM params per step, output.
- **R9.2**: Events: `agent_started`, `agent_step`, `agent_reflect`, `agent_done`, `agent_forced_done`, `agent_spawned`, `agent_stream`, `agent_tool_result`, `param_adjusted`, `stuck_detected`, `condensation_triggered`, `budget_threshold_hit`.
- **R9.3**: Full execution tree reconstructable from events: who spawned whom, what was handed off, what params were adjusted.
- **R9.4**: Token accounting per-agent and aggregated.
- **R9.5**: LLM param trajectory logged per agent — every change with timestamp and source.

---

## 10. What the Framework Does NOT Do

- **R10.1**: No predefined topologies, DAGs, pipelines, or state machines. Agents create structure at runtime through tool calls.
- **R10.2**: No automatic parameter schedules, curves, or drivers. LLM params are mutable state controlled by agents or pushers.
- **R10.3**: No declarative feedback loops between agents. A supervisor agent with tools IS the feedback loop.
- **R10.4**: No auto-fork, auto-spawn, or auto-plan configs. If you want automatic behavior, encode it in the agent's prompt.
- **R10.5**: No persistent state between runs. Each execution is stateless.
- **R10.6**: No web UI. CLI and Python API only.
- **R10.7**: No multi-provider abstraction beyond OpenAI-compatible interface.

---

## 11. Summary: Design Principles

| Principle | Implication |
|---|---|
| **Agent-first** | The agent is the only execution unit. No topology runner, no pipeline engine. |
| **Tools, not config** | Spawn, fork, plan, adjust — all via tool calls, not YAML declarations. |
| **Prompts, not pipelines** | Agent behavior is determined by prompts. The framework imposes no workflow. |
| **Mutable params** | LLM generation parameters are live state, not static config. Other agents can change them. |
| **Max non-determinism** | The react loop has no predetermined steps. The LLM decides everything at every step. |
| **Supervisor = agent** | Feedback loops, adaptive schedules, coordination — all via a supervisor agent with `adjust_agent` + `observe_agents` tools and a prompt. |
| **Signals, not actions** | The framework computes behavioral signals (repetition, stuck) but does not act on them. Agents or pushers decide. |
| **Budget = only hard constraint** | Budget pushers are the only framework-imposed guardrails. Everything else is soft (prompt-based). |
