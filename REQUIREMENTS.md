# DiffGraph Agent Orchestration Framework — Technical Requirements

## 1. Core Abstractions

### 1.1 Agent Definition

An **Agent** is the atomic unit. Each agent is defined by a config (YAML or dict) and is runtime-instantiable.

```yaml
agents:
  security_reviewer:
    system_prompt: prompts/security.txt
    sgr: true                          # enables reflect() tool + question tracking
    tools: [find_files, read_file, read_outline, search, get_diff]
    spawn_tools: [spawn_agent]         # optional: can fork
    output_schema: ReviewFinding[]     # structured output on done()
    budget:
      max_tokens: 20000
      max_steps: 25
      max_wall_time: 120s              # optional time-based limit
    budget_pushers:                     # configurable nudge thresholds
      - at: 0.5
        type: nudge
        message: "Focus on high-severity issues only."
      - at: 0.75
        type: nudge
        message: "Wrap up. Call done() soon."
      - at: 0.9
        type: force_reflect            # force a reflect() before continuing
      - at: 1.0
        type: force_done
    condensation:                      # for long-running agents
      enabled: true
      trigger: 30000                   # tokens in message history
      strategy: llm_summary            # or: drop_tool_results | sliding_window | hybrid
      preserve_last: 5                 # always keep last N messages uncondensed
      preserve_sgr: true               # always keep reflect() calls intact

  perf_reviewer:
    system_prompt: prompts/performance.txt
    sgr: false                         # non-SGR agent, simpler loop
    tools: [find_files, read_file, search, get_diff]
    output_schema: ReviewFinding[]
    budget:
      max_tokens: 15000
      max_steps: 15

  synthesizer:
    system_prompt: prompts/synthesizer.txt
    sgr: false
    tools: []                          # no repo tools, just receives data
    output_schema: ReviewFinding[]
    budget:
      max_tokens: 10000
      max_steps: 3
```

**Requirements:**

- **R1.1**: Agent configs loadable from YAML files or inline dicts.
- **R1.2**: Agents are either **SGR** (have `reflect()` with question tracking) or **non-SGR** (plain ReAct or single-shot).
- **R1.3**: Each agent has its own independent budget (tokens, steps, wall time) — not shared.
- **R1.4**: Budget pushers are a configurable list of `(threshold, action)` pairs, not hardcoded.
- **R1.5**: Pusher actions: `nudge` (inject user message), `force_reflect` (only allow reflect tool next step), `force_done` (only allow done tool), `custom` (call a Python hook).
- **R1.6**: Agents are instantiated at runtime from a registered name — not imported as classes.

### 1.2 Topology Definition

A **Topology** defines how agents connect. It is a DAG (directed acyclic graph at definition time, but dynamic spawning can create runtime branches).

```yaml
topologies:
  parallel_review:
    nodes:
      - id: strategist
        agent: strategist
        type: single                   # one-shot, no ReAct

      - id: reviewers
        agent: $dynamic                # agent chosen at runtime by strategist
        type: fan_out
        source: strategist             # takes output from strategist
        parallel: true
        spawn_from: plan.tasks         # one agent per task in plan output

      - id: synthesizer
        agent: synthesizer
        type: join
        sources: [reviewers]           # waits for all fan_out agents

    edges:
      - from: strategist
        to: reviewers
        context_handoff: sgr_outcomes  # what to pass (see 1.3)

      - from: reviewers
        to: synthesizer
        context_handoff: findings_and_sgr
```

**Requirements:**

- **R1.7**: Topologies are declarative DAGs defined in YAML or constructed programmatically.
- **R1.8**: Node types: `single` (one-shot LLM call), `react` (ReAct loop), `fan_out` (spawn N parallel agents), `join` (wait and aggregate).
- **R1.9**: Fan-out cardinality can be: static (N defined in config), dynamic (derived from parent output field like `plan.tasks`), or agent-decided (see 1.4).
- **R1.10**: Topologies are selectable at runtime (e.g., `--topology parallel_review`).
- **R1.11**: Topologies can be nested — a node can reference another topology as a sub-graph.

### 1.3 Context Handoff Modes

When one agent passes context to another, the **handoff mode** controls what is transferred.

```yaml
context_handoff_modes:
  full_history:          # fork with complete message history
    messages: all

  sgr_outcomes:          # only the last reflect() output
    messages: none
    include: [last_sgr]

  findings_and_sgr:      # structured output + full SGR history
    messages: none
    include: [output, all_sgr]

  condensed:             # LLM-summarized history
    messages: condensed
    condense_prompt: "Summarize the investigation so far in <500 words."

  last_n:                # sliding window
    messages: last_20
    include: [first_sgr, last_sgr]   # bookend with first and last reflection

  custom:                # user-defined Python function
    handler: myproject.handoffs.security_handoff
```

**Requirements:**

- **R1.12**: Context handoff is configured per-edge in the topology, not globally.
- **R1.13**: Built-in handoff modes:
  - `full_history` — clone the entire message list (fork semantics).
  - `sgr_outcomes` — only the last `reflect()` call's structured data.
  - `all_sgr` — all `reflect()` calls in order (the full reasoning trajectory).
  - `findings_only` — only the `done()` output, no history.
  - `findings_and_sgr` — done() output + all reflect() calls.
  - `condensed` — LLM-generated summary of message history (configurable prompt).
  - `last_n` — last N messages, optionally with SGR bookends.
  - `custom` — user-provided Python callable `(messages, sgr_history, output) → messages`.
- **R1.14**: Handoff modes are composable — e.g., `include: [output, last_sgr, last_5_messages]`.
- **R1.15**: Join nodes receive a **list** of handoffs (one per source agent), not a merged blob — the synthesizer prompt can reference them individually.

### 1.4 Agent-Driven Topology (Dynamic Spawning)

Agents with spawn capability can create child agents at runtime via three mechanisms.

**Mechanism A: Tool call**

```json
{
  "name": "spawn_agent",
  "arguments": {
    "agent": "security_reviewer",
    "focus": "Check if the new endpoint validates auth tokens",
    "context_handoff": "sgr_outcomes",
    "wait": true
  }
}
```

**Mechanism B: JSON output field**

```json
{
  "spawn": [
    {"agent": "security_reviewer", "focus": "auth validation"},
    {"agent": "perf_reviewer", "focus": "N+1 query in the new loop"}
  ]
}
```

**Mechanism C: SGR-driven (from `reflect()` questions)**

```yaml
agents:
  deep_researcher:
    sgr: true
    auto_fork:
      enabled: true
      trigger: questions_remaining    # fork when questions > threshold
      threshold: 3                    # if ≥3 open questions after reflect
      strategy: one_per_question      # spawn one child per question
      child_agent: focused_researcher
      context_handoff: sgr_outcomes
      max_children: 3
      depth_limit: 2
```

**Requirements:**

- **R1.16**: Three spawn mechanisms: tool call, JSON output field, SGR-driven auto-fork.
- **R1.17**: Each spawn specifies: which agent config, focus/prompt override, context handoff mode, sync/async.
- **R1.18**: **Synchronous spawn** (`wait: true`): parent blocks, child runs, child result injected into parent's message history as a tool result. Parent continues its ReAct loop.
- **R1.19**: **Asynchronous spawn** (`wait: false`): parent continues, child runs in parallel. Results collected at next join node or at parent's `done()`.
- **R1.20**: Depth limit is enforced globally — agents at max depth have spawn tools removed from their toolset.
- **R1.21**: SGR auto-fork is configurable: trigger condition, threshold, child agent, max children per fork event.
- **R1.22**: An agent can create a **sub-topology** via a tool call — returning a topology definition (YAML or dict) that the runtime executes and returns results from.

## 2. Budget & Lifecycle

### 2.1 Budget Model

```yaml
budget:
  max_tokens: 40000          # total token consumption (in + out)
  max_steps: 40              # ReAct loop iterations
  max_wall_time: 180s        # real time limit
  max_children_budget: 0.3   # max fraction of own budget allocatable to children

  pushers:
    - at: 0.5                # 50% of any limit
      type: nudge
      message: "Half budget used. Prioritize."
    - at: 0.75
      type: force_reflect    # must reflect before next action
    - at: 0.9
      type: nudge
      message: "Almost out of budget. Call done() with current findings."
    - at: 1.0
      type: force_done
```

**Requirements:**

- **R2.1**: Budget tracked on three dimensions independently: tokens, steps, wall time.
- **R2.2**: Pushers trigger on whichever dimension hits the threshold first.
- **R2.3**: Pusher types: `nudge`, `force_reflect`, `force_done`, `custom` (Python callback).
- **R2.4**: Pushers are evaluated after every step, not just at LLM call boundaries.
- **R2.5**: When an agent spawns children, budget is partitioned: `child_budget = min(remaining * allocation_fraction, max_children_budget * original_budget)`.
- **R2.6**: Parent's budget is debited by child's actual consumption, not the allocated amount.
- **R2.7**: Budget is a first-class object passed through the topology, not implicit global state.

### 2.2 Message Condensation

For long-running agents whose message history grows beyond useful context window.

**Requirements:**

- **R2.8**: Condensation triggered when message history exceeds a configurable token threshold.
- **R2.9**: Condensation strategies:
  - `llm_summary` — separate LLM call to summarize older messages, replace them with summary.
  - `drop_tool_results` — keep tool calls but truncate results to first N chars.
  - `sliding_window` — keep only last N messages, prepend a static summary of dropped messages.
  - `hybrid` — LLM-summarize messages older than N, keep recent ones verbatim.
- **R2.10**: SGR reflect() calls are optionally exempt from condensation (`preserve_sgr: true`) — they are the agent's memory backbone.
- **R2.11**: System message and first user message are never condensed.
- **R2.12**: Condensation is transparent to the agent — it doesn't know its history was condensed.

## 3. Tool System

### 3.1 Tool Registry

```python
@tool_registry.register
def read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    """Read up to 100 lines of a file."""
    ...
```

Or declaratively:

```yaml
tools:
  read_file:
    handler: diffgraph.tools.read_file
    description: "Read up to 100 lines of a file."
    parameters:
      path: {type: string, required: true}
      start_line: {type: integer}
      end_line: {type: integer}
    result_limit: 6000            # auto-truncate result
```

**Requirements:**

- **R3.1**: Tools registered via decorator or YAML — both produce the same internal schema.
- **R3.2**: Each agent's toolset is configured in its agent definition — not global.
- **R3.3**: Special tools (`reflect`, `done`, `spawn_agent`) are added automatically based on agent config flags (`sgr: true`, `spawn_tools`, etc.).
- **R3.4**: Tool results auto-truncated at configurable limit per tool.
- **R3.5**: Tools execute in parallel when the LLM returns multiple tool calls in one response (current behavior preserved).

### 3.2 Shared / Collaborative Tools (Swarm)

For agents that need to coordinate via shared state rather than just message passing.

```yaml
shared_tools:
  shared_findings:
    type: append_log              # agents append, all can read
    schema: ReviewFinding

  claim_board:
    type: mutex_map               # agents claim files to avoid duplicate work
    schema: {file: string, agent: string}
```

**Requirements:**

- **R3.6**: Shared tools are declared at topology level, injected into specified agents.
- **R3.7**: Shared tool types:
  - `append_log` — any agent can append, any agent can read all entries. Thread-safe.
  - `mutex_map` — agents claim keys (e.g., file paths). `claim(key)` fails if already claimed by another agent. Prevents duplicate work.
  - `blackboard` — key-value store, any agent reads/writes. Last-write-wins.
  - `custom` — user-provided Python class implementing `read()`, `write()`, `query()`.
- **R3.8**: Shared tool state is scoped to a single topology execution — not persisted across runs.
- **R3.9**: Agents interact with shared tools via normal tool calls — the LLM sees them as regular tools with descriptions like "Read all findings submitted by other reviewers".

## 4. SGR (Self-Guided Reasoning) System

**Requirements:**

- **R4.1**: SGR is an opt-in capability per agent (`sgr: true`).
- **R4.2**: SGR-enabled agents get `reflect()` tool with current schema: `learned`, `questions_remaining`, `resolved_questions`, `confidence`, `next_action`.
- **R4.3**: SGR frequency is configurable: `sgr_interval: 3` means the system nudges reflection every 3 steps (current behavior).
- **R4.4**: SGR history (all reflect calls) is a first-class data structure attached to the agent's execution record — extractable for handoff, join, and observability.
- **R4.5**: SGR can be extended with custom fields per agent:
  ```yaml
  sgr_extensions:
    risk_assessment: {type: string, enum: [low, medium, high, critical]}
    files_analyzed: {type: array, items: {type: string}}
  ```
- **R4.6**: SGR-driven auto-fork (R1.21) reads `questions_remaining` to decide when/how to spawn children.

## 5. Agent-Created Topologies

An agent (typically the strategist) can output a topology definition rather than just a task list.

**Requirements:**

- **R5.1**: A `create_topology` tool or JSON output field allows an agent to define a sub-topology at runtime.
- **R5.2**: The runtime validates the topology against registered agent names and available tools before executing.
- **R5.3**: The agent-created topology is subject to the parent's remaining budget — it cannot allocate more than what's left.
- **R5.4**: Agent-created topologies support all node types: single, react, fan_out, join.
- **R5.5**: This replaces the current strategist's static `tasks[]` output with a richer structure:
  ```json
  {
    "topology": {
      "nodes": [
        {"id": "security", "agent": "security_reviewer", "focus": "..."},
        {"id": "logic", "agent": "logic_reviewer", "focus": "..."},
        {"id": "merge", "agent": "synthesizer", "sources": ["security", "logic"]}
      ],
      "edges": [
        {"from": "security", "to": "merge", "context_handoff": "findings_and_sgr"},
        {"from": "logic", "to": "merge", "context_handoff": "findings_only"}
      ]
    }
  }
  ```
- **R5.6**: Predefined topology templates can be referenced by name — the strategist doesn't have to build from scratch every time.

## 6. Forking Behavior (Explore Both Paths)

When an agent is uncertain, it should be able to fork itself to explore alternatives in parallel.

```yaml
agents:
  explorer:
    sgr: true
    fork:
      enabled: true
      mechanism: tool                  # or: sgr_auto | json_output
      max_forks: 2
      budget_split: equal              # or: proportional | fixed
      context_handoff: full_history    # forks get same history
      merge_strategy: best_confidence  # or: union | llm_merge | custom
```

**Requirements:**

- **R6.1**: An agent can fork itself into N parallel copies, each pursuing a different hypothesis.
- **R6.2**: Fork trigger is configurable: explicit tool call (`fork(branches: [{focus: "..."}, ...])`), SGR auto-fork when confidence is low + multiple plausible next actions, or JSON output.
- **R6.3**: Budget split strategies: `equal` (remaining / N), `proportional` (weighted by priority), `fixed` (each gets a fixed amount).
- **R6.4**: Context at fork point: configurable handoff mode (typically `full_history` for true forking, or `sgr_outcomes` for lightweight branching).
- **R6.5**: Merge strategies for collecting fork results:
  - `best_confidence` — take results from the fork with highest SGR confidence.
  - `union` — concatenate all findings, deduplicate.
  - `llm_merge` — run a merge agent to reconcile conflicting findings.
  - `custom` — user-provided Python callable.
- **R6.6**: Fork depth limit enforced (default 1 — forks cannot fork again unless explicitly allowed).

## 7. Adaptive LLM Parameters & Feedback Loops

LLM generation parameters (temperature, top_p, penalties, max tokens, even model) are not static — they are **dynamic variables** driven by agent state, budget, and behavioral signals.

### 7.1 Parameter Scheduling

Analogous to learning rate scheduling in ML training. Parameters follow a **schedule** that can be state-driven, rule-driven, or both.

```yaml
agents:
  deep_researcher:
    llm_params:
      # --- static defaults ---
      model: gpt-4o
      temperature: 0.3
      top_p: 1.0
      frequency_penalty: 0.0
      presence_penalty: 0.0
      max_completion_tokens: 4096

      # --- adaptive schedules ---
      schedules:
        # Budget-driven: become more focused as budget depletes
        - param: temperature
          driver: budget_ratio                # 0.0 = fresh, 1.0 = exhausted
          curve: linear_decay                 # or: step | exponential_decay | custom
          range: [0.4, 0.1]                   # [start_value, end_value]

        # SGR-driven: explore more when confidence is low
        - param: temperature
          driver: sgr_confidence              # low=0, medium=0.5, high=1.0
          curve: inverse_linear
          range: [0.6, 0.1]                   # low confidence → 0.6, high → 0.1

        # Step-driven: shorter responses in later steps
        - param: max_completion_tokens
          driver: step_ratio
          curve: step
          steps: {0.0: 4096, 0.5: 2048, 0.8: 1024}

        # Anti-repetition: increase penalty when stuck
        - param: frequency_penalty
          driver: repetition_score            # 0.0 = novel, 1.0 = repeating
          curve: linear
          range: [0.0, 1.5]
```

**Requirements:**

- **R7.1**: LLM parameters (`temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `max_completion_tokens`) are adjustable per-step, not just per-agent.
- **R7.2**: Parameter schedules are configurable per agent in YAML or via Python API.
- **R7.3**: Built-in schedule drivers (signal sources):
  - `budget_ratio` — fraction of budget consumed (0.0 → 1.0). Works for tokens, steps, or wall time (whichever is highest).
  - `step_ratio` — current step / max steps.
  - `sgr_confidence` — from last reflect(): `low`=0.0, `medium`=0.5, `high`=1.0.
  - `sgr_question_count` — number of open questions remaining.
  - `sgr_staleness` — number of reflect() calls since confidence last changed.
  - `repetition_score` — computed from recent tool calls (see R7.7).
  - `custom` — user-provided Python callable `(agent_state) → float`.
- **R7.4**: Built-in curve functions:
  - `linear` / `linear_decay` — linear interpolation between range endpoints.
  - `step` — discrete steps at defined thresholds.
  - `exponential_decay` — fast decay early, slow later.
  - `inverse_linear` — flip the interpolation direction.
  - `custom` — user-provided `(driver_value, range) → param_value`.
- **R7.5**: When multiple schedules target the same parameter, a **merge policy** resolves conflicts: `min`, `max`, `mean`, `last_wins`, or `custom`.
- **R7.6**: Parameter overrides from schedules are logged in the agent trace for observability.

### 7.2 Behavioral Feedback Signals

The system computes real-time behavioral signals from the agent's execution history. These feed into parameter schedules and budget pushers.

```yaml
feedback_signals:
  repetition_score:
    window: 6                          # look at last 6 tool calls
    triggers:
      - condition: same_tool_3x        # same tool called 3+ times in window
        weight: 0.5
      - condition: same_args_2x        # same tool+args called 2+ times
        weight: 0.8
      - condition: same_file_read_3x   # same file read 3+ times
        weight: 0.9

  progress_score:
    signals:
      - sgr_questions_resolved_rate    # questions resolved per reflect()
      - unique_files_explored_rate     # new files per step
      - findings_rate                  # findings discovered per step

  stuck_detector:
    window: 8
    condition: repetition_score > 0.6 AND progress_score < 0.2
    actions:
      - type: nudge
        message: "You appear to be going in circles. Reflect on what you've learned and try a different approach."
      - type: adjust_param
        param: temperature
        value: 0.7
      - type: adjust_param
        param: presence_penalty
        value: 1.0
```

**Requirements:**

- **R7.7**: The runtime computes a **repetition score** from a sliding window of recent tool calls. Configurable: window size, what counts as repetition (same tool, same args, same file).
- **R7.8**: The runtime computes a **progress score** from SGR question resolution rate, unique files explored, and findings produced per step.
- **R7.9**: A **stuck detector** combines signals to detect unproductive loops. Configurable threshold and window.
- **R7.10**: When stuck is detected, configurable actions fire: nudge message, parameter adjustment, force reflect, or custom hook.
- **R7.11**: Feedback signals are available as schedule drivers (R7.3) — they can drive any parameter, not just trigger discrete actions.
- **R7.12**: All behavioral signals are exposed in the agent trace and via `on_event` for observability.

### 7.3 Dynamic Model Switching

```yaml
agents:
  adaptive_reviewer:
    llm_params:
      model: gpt-4o                    # default model
      model_schedule:
        - phase: tool_calls            # routine tool use steps
          model: gpt-4o-mini           # cheaper model for simple dispatch
          condition: tool_count == 1 AND tool_name in [find_files, read_file, search]
        - phase: reflection            # SGR reflect steps
          model: gpt-4o               # full model for reasoning
        - phase: synthesis             # done() step
          model: gpt-4o               # full model for final output
        - phase: stuck                 # when stuck detector fires
          model: gpt-4o               # upgrade if on mini, or switch provider
```

**Requirements:**

- **R7.13**: Model can change per-step based on configurable conditions (phase, tool pattern, stuck state, budget).
- **R7.14**: Model switching is transparent to the agent — message history format remains compatible across models.
- **R7.15**: Token budget accounting normalizes across models (e.g., a mini step costs fewer tokens than a full-model step, tracked accurately).

### 7.4 Feedback Loops Between Agents

Parent agents can adjust child agent parameters based on intermediate results.

```yaml
topologies:
  adaptive_review:
    feedback_loops:
      - observer: synthesizer          # agent that monitors
        targets: [reviewers]           # agents being adjusted
        trigger: on_child_reflect      # fires when a child calls reflect()
        condition: child.sgr.confidence == "low" AND child.step_ratio > 0.5
        actions:
          - type: adjust_param
            param: temperature
            value: 0.5
          - type: inject_message
            message: "The synthesizer notes your confidence is low at 50% budget. Consider narrowing scope."
          - type: extend_budget
            steps: +5                  # give the struggling agent more runway
```

**Requirements:**

- **R7.16**: Feedback loops are declared at the topology level, connecting an observer agent (or the runtime itself) to target agents.
- **R7.17**: Triggers: `on_child_reflect`, `on_child_step`, `on_child_stuck`, `on_child_done`, `periodic` (every N steps).
- **R7.18**: Actions: `adjust_param`, `inject_message`, `extend_budget`, `reduce_budget`, `force_reflect`, `force_done`, `custom`.
- **R7.19**: Budget extension/reduction by feedback loops is bounded — a configurable `max_feedback_budget_delta` prevents runaway allocation.
- **R7.20**: Cross-agent feedback is logged as events and visible in the execution trace.

## 8. Configurability & Extensibility (General)

**Requirements:**

- **R8.1**: All agent definitions, topologies, budget configs, handoff modes, and parameter schedules are YAML-configurable.
- **R8.2**: Python API for everything YAML can do — YAML is syntactic sugar, not the only interface.
- **R8.3**: Prompt templates support variable interpolation: `{diff_summary}`, `{plan}`, `{parent_sgr}`, etc.
- **R8.4**: Adding a new agent = adding a YAML block + optional prompt file. No code changes to the framework.
- **R8.5**: Adding a new tool = decorator or YAML registration. No changes to agent loop code.
- **R8.6**: Changing topology = editing YAML or calling `topology.add_node()` / `topology.add_edge()`. No changes to execution engine.
- **R8.7**: Custom budget pushers via Python hooks: `def my_pusher(agent_state, budget_state) → PusherAction`.
- **R8.8**: Custom handoff modes via Python hooks: `def my_handoff(messages, sgr_history, output) → messages`.
- **R8.9**: Custom merge strategies via Python hooks: `def my_merge(fork_results) → merged_output`.
- **R8.10**: Custom parameter schedule drivers and curves via Python hooks.
- **R8.11**: Custom feedback signal computations via Python hooks.

## 9. Observability

**Requirements:**

- **R9.1**: Every agent execution produces a structured trace: agent_id, parent_id, topology_node, steps, tools called, SGR history, budget consumed, LLM params per step, output.
- **R9.2**: The event system (current `on_event`) is extended with: `agent_spawned`, `agent_done`, `fork_started`, `fork_merged`, `condensation_triggered`, `budget_threshold_hit`, `param_adjusted`, `stuck_detected`, `feedback_loop_fired`.
- **R9.3**: Full execution tree is reconstructable from events — which agent spawned which, what was handed off, what was merged, what parameters were adjusted.
- **R9.4**: SGR convergence tracking per agent (current live display) extends to show the full agent tree.
- **R9.5**: Token accounting is per-agent and aggregated — you can see budget consumption at any level.
- **R9.6**: Parameter trajectory is logged — you can plot temperature, penalties, etc. over time for each agent.

## 10. Non-Requirements (Explicitly Out of Scope)

- **R10.1**: No persistent state between runs — each review is stateless.
- **R10.2**: No built-in A/B testing framework (but topology selection enables manual A/B).
- **R10.3**: No built-in prompt optimization (complement with DSPy externally).
- **R10.4**: No multi-LLM-provider abstraction beyond current OpenAI-compatible interface — agents can use different models via config but through the same client interface.
- **R10.5**: No web UI — CLI and Python API only.

## 11. Summary: Current State vs. Proposed

| Current (1,900 LOC) | Proposed |
|---|---|
| 1 strategist + 1 solver, hardcoded | N agents, YAML-defined |
| Fixed 2-phase pipeline | Arbitrary DAG topologies |
| Hardcoded 50%/75% nudges | Configurable budget pushers per agent |
| No spawning | 3 spawn mechanisms + auto-fork from SGR |
| No context control | 7+ handoff modes, per-edge configurable |
| No condensation | 4 condensation strategies for long-running agents |
| No shared state | Swarm tools (append_log, mutex_map, blackboard) |
| SGR is one schema | SGR with custom extensions per agent |
| Topology is code | Topology is YAML or agent-created at runtime |
| Static temperature=0 | Adaptive LLM params driven by SGR, budget, and behavioral signals |
| No repetition detection | Stuck detector with configurable feedback actions |
| Single model throughout | Dynamic model switching per step based on phase/conditions |
| No cross-agent feedback | Topology-level feedback loops between parent and child agents |
