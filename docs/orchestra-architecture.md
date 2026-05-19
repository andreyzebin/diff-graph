# Orchestra — architecture

`orchestra/` is the model-agnostic agent framework. It owns the
ReAct loop, tool dispatch, schema validation, condensation, OTel
tracing, and prompt compilation. Domain code (`diffgraph/`,
`quality_api/`, the QA stack) plugs in by registering tools and
loading a prompt directory; orchestra runs the loop.

This doc is a map: where the moving parts live and how they
connect. For per-component details, follow the file pointers.

## 1. Run, top-down

A single agent call looks like this:

```
caller (cli.py / diffgraph.api / quality_cli)
   │
   ▼
run_agent(agent_name, data, llm, tool_registry, ...)              [orchestrator]
   │
   ▼
compile_prompts(...)  →  AgentConfig                              [orchestra.compiler]
   │                       (system_prompt, tools, budget,
   │                        llm_params, …)
   ▼
Agent(config, tool_registry, llm, ...)                            [orchestra.agent]
   │
   ▼
ReAct loop:
   for step in 0..budget.max_steps:
     1. assemble messages = [system] + [user] + history
     2. call LLM (orchestra.streaming.llm_call_streaming)
        → assistant message: {content, tool_calls}
     3. for each tc in tool_calls:
          args = json.loads(tc.arguments)
          registry.dispatch(tc.name, args, raw_args=tc.arguments)
     4. fold tool results back into history
     5. apply pushers (token budget, time budget, reflect cadence)
     6. check stop conditions (done(), max_steps, max_tokens)
```

The Agent itself is dumb about *what* tools do — it just routes
JSON through the registry and feeds the results back. Each layer
below adds a feature without coupling to the loop.

## 2. Layers, by responsibility

### `orchestra/types.py` — config dataclasses

Single source of truth for `AgentConfig`, `BudgetConfig`,
`LLMParamsConfig`, `CondensationConfig`. Every other module
consumes these.

- `AgentConfig.tools`: the tool names this agent may invoke.
- `AgentConfig.budget`: `BudgetConfig` (max_tokens, max_steps,
  pushers).
- `AgentConfig.llm_params`: `LLMParamsConfig` (model, temperature,
  tool_choice, stream, extra_body, `fix_qwen3_stringification_bug`).
- `AgentConfig.mode`: `react` (tool-using loop) or `single`
  (one-shot text answer, used by judges).
- `AgentConfig.reflect_response_template`: short template name
  (default `"default"`) for the `reflect` tool's tool-result. See
  `orchestra/reflect_response.py` below — opt-in `with_state`
  carries a live budget+time+children snapshot so the agent
  re-grounds planning on every reflect, without calling a separate
  query tool.

### `orchestra/compiler.py` + `orchestra/prompts/` — prompt → config

Prompts are `.md` files under a prompt directory with YAML
frontmatter declaring `tools`, `budget`, etc. The compiler walks
the dir, parses frontmatter via `orchestra/prompts/frontmatter.py`,
and produces an `AgentRegistry` (mapping `agent_name → AgentConfig`).

- `orchestra/prompts/internal/` — framework-built-in prompts
  shared across deployments (reflect-nudge templates, condensation
  prompt, etc.).
- A caller's prompt directory (e.g. `diffgraph/prompts/`) is
  passed via `prompt_resource=...`. Domain prompts override or
  extend the framework defaults.

The compiler also surfaces **data** fields declared by the prompt:

```yaml
data:
  pr_title:       { type: string }
  comment_thread: { type: string }
  commits:        { from_tool: pr_context, from_field: commits }
```

`from_tool` makes a field lazy — the value comes from a registered
data-provider tool (`hidden=True, cache=True`) when the agent
spawns. See `orchestra/agent.py:resolve_agent_data`.

### `orchestra/agent.py` — the ReAct loop

`Agent` owns one prompt run. Key responsibilities:

- Assemble messages each step (system + user + history + any
  pusher-injected nudges).
- Call the LLM via `orchestra/streaming.py` (OpenAI-compatible
  endpoint with streaming support).
- Dispatch tool calls through the registry — see §4.
- Fold tool results back as `role=tool` messages.
- Emit `EventType.*` to `event_bus` so listeners (`TraceCollector`,
  trace-DB writer, OTel exporter) can record state.
- Apply pushers (`apply` pre-LLM, `on_step_done` post-dispatch) —
  see §6.
- Honour `mode: single` for judges / mode:single agents:
  one LLM call, no tools, no loop.

Per-step contract: each iteration emits exactly one
`AGENT_LLM_REQUEST` + one `AGENT_LLM_RESPONSE` event, plus N
`AGENT_TOOL_REQUEST` / `AGENT_TOOL_RESULT` events for the N tool
calls in that step. The trace tree leans on this 1:1:N shape.

### `orchestra/tools/registry.py` — tool registry + dispatch

`ToolRegistry` is the central tool catalogue. Tools register via:

- decorator: `@registry.register(name=..., description=...,
  parameters=..., result_limit=..., hidden=..., cache=...)`
- direct call: `registry.register_tool_def(ToolDef(...))`
- YAML: `registry.register_from_yaml(...)`

Each `ToolDef` carries name, description, JSON Schema, the handler
callable, and a few framework hints (`hidden` to drop from the
agent-visible list, `cache=True` for data providers, `is_builtin`
for `reflect`/`done`/`agent_spawn`).

`registry.to_openai_schema(names)` produces the `tools=[...]` array
the LLM sees. `registry.dispatch(name, args, raw_args=...)`:

1. Look up the `ToolDef`.
2. Validate args against the schema (`_validate_args`).
3. On validation failure, walk the **argument-repair chain** —
   see §5.
4. On mock interception (test fixtures), short-circuit with the
   canned result.
5. Call the handler with `**args`.
6. Truncate the result to `td.result_limit` and return.

#### Tool-result convention: report state, don't dictate the next move

When a tool can't do its job — disabled, unconfigured, the resource
is gone — its result should **name the state plainly and say
"proceed", then stop**. It must NOT prescribe what the agent should
fall back on ("…proceed with the diff + PR description alone").

Why: a general-purpose tool doesn't know what task it's serving.
The prompt already frames what the agent is doing; a tool result
that re-narrates the fallback is both redundant and brittle — it
bakes one caller's task shape into a tool every caller shares, and
it quietly competes with the prompt for control of the agent's
next step. The tool's job is to be honest about *its own* status;
choosing the fallback is the agent's judgment, set by the prompt.

Concrete examples in-tree: the Jira `jira_read_ticket` sentinels
(`_disabled` / `_not_configured` / `_not_viewable` in
`diffgraph/providers/jira.py`) and the `disable-*.yaml` mock
fixtures — `"Jira integration is disabled for this run — proceed."`,
not `"… — proceed with the diff alone."`. Same rule for the
`tool_mocks.py` canned strings: state the mock's effect, leave the
"what now" to the prompt.

The convention extends to **budget pushers** (`orchestra/budget.py`).
A pusher whose remedy isn't universal across agents — e.g. the
`ContextBudgetPusher` whose "right answer" depends on whether the
agent has `agent_spawn`, whether it can condense, whether it's a
leaf — should:

- **Report state plainly** in its NUDGE message ("Context at 50% of
  the effective window." — not "prefer agent_spawn", not "plan to
  wrap up").
- **Skip FORCE_DONE** when narrowing tools would itself be a
  prescriptive action. Set `DEFAULT_FORCE_DONE_AT = None` so the
  axis stays NUDGE-only; the prompt picks the remedy.

Token / step / time pushers DO keep FORCE_DONE — those dimensions
have a universal endpoint (the run actually can't continue) where
narrowing to `done` is the correct architectural floor, not a
preference. Context-window pressure has multiple legitimate
remedies (spawn, condense, wrap up) and no universal answer, so its
pusher stays informational.

### `orchestra/tools/builtin.py` — framework-provided tools

Registers `reflect`, `done`, `agent_spawn`, `agent_list`,
`budget_stats` when the prompt asks for them in its `tools:` list.
Each builtin gets a schema derived from the agent's config (e.g.
`done`'s `findings` field type comes from
`agent_config.output_schema`; `reflect`'s fields are augmented
with `sgr_extensions`).

The reflect handler **only fires after** dispatch's
`_validate_args` accepts the call. That's why malformed reflects
(qwen3-spam pattern) don't silently reset the cadence counter.

The reflect handler's **return string** is delegated to
`orchestra/reflect_response.py::render(...)` — by default it
returns the plain `"Reflection noted."` (current behavior,
load-bearing backward compat). When the agent's prompt opts into a
richer template via `reflect_response_template:` frontmatter, the
handler snapshots `_children` under `_children_lock` and passes
that plus `budget_state` through to the renderer. The default-name
fast-path stays cheap (no children snapshot, no interpolation).

### `orchestra/reflect_response.py` + `orchestra/budget_stats.py` — internal-API render layer

Where `reflect` and `budget_stats` get the **content** they return
to the agent. Both follow the same shape:

- **Internal-API functions** (pure, mockable like `fake_bitbucket`
  helpers): `format_budget_stats(state, children)`,
  `format_time_info(state)`. Each takes the agent's `BudgetState`
  and renders a single block of text.
- **Template files** in `orchestra/templates/<surface>/<name>.md`
  reference internal APIs by `{placeholder}` name. Renderer loads
  the file and `format(...)`-substitutes each placeholder. Wording
  / structure can be tuned without code changes.
- **Per-prompt toggle**: `AgentConfig.reflect_response_template`
  selects which template the reflect handler uses. Default name
  `"default"` is `"Reflection noted."`; opt-in `"with_state"`
  composes `{time_info}` + `{budget_stats}` for continuous
  awareness without an explicit query call.

The `budget_stats` *tool* is the on-demand version of the same
data — kept callable for tests and for prompts that don't opt into
`with_state` reflects. Both surfaces share the same
`format_budget_stats` source of truth, so there's no risk of two
divergent texts describing the same numbers.

**Adding a new internal API**:
1. Write `format_X(state, ...) -> str` in its own module.
2. Add `X` as a property on `orchestra/runcontext.py::RunContext`
   so the Jinja engine sees it in `ctx.to_kwargs()`.
3. Reference `{{ X }}` in a new template file under
   `orchestra/templates/reflect_response/`.

A missing template file falls back to `"Reflection noted."` so a
typo in the toggle never crashes an agent.

### `orchestra/tools/meta.py` — framework escape-hatches

`agent_spawn`, `agent_list`, `pr_context` (data provider). These
hook into `Agent` methods directly (the registry maps them through
small bridge handlers).

### `orchestra/sgr.py` — Self-Guided Reasoning

`SGRTracker` is the structured-reflect state machine. It builds
the `reflect()` JSON schema (`learned`, `questions_remaining`,
`resolved_questions`, `confidence`, `next_action`, plus any
prompt-declared `sgr_extensions`), records each reflect call into
a history list, and provides `extract_for_handoff` for child-agent
prompting.

#### What reflect actually does — the conceptual model

`reflect` is a **convergence aid for investigative multi-step
problems**, not a logging tool. Each call is a checkpoint that
externalises the agent's working state so it survives the next
step's prompt rebuild instead of living only in working memory
and getting crowded out as context grows.

The five fields aren't decorative — each one defends against a
specific failure mode of long LLM chains:

| Field | Defends against | How |
|---|---|---|
| `learned` | **Drift** — partial findings vanishing as context grows | Anchors facts as plain text that the next step's prompt sees verbatim. One line per fact; if it can't be stated in a line, the agent doesn't actually know it yet. |
| `questions_remaining` (with stable IDs) | **Loops** — re-asking what was already answered | Each question gets a short ID. Later reflects close by ID, not by re-typing prose. Re-opening the same ID is a flag. |
| `resolved_questions` | **Premature termination** ("I don't know if I'm making progress") | Closures by ID create a progress signal. The ratio of closed-vs-still-open over successive reflects tells the budget layer whether the agent is converging or spinning. |
| `confidence` | **Mis-calibration** — wrapping up while still uncertain, or thrashing while actually near the answer | `low`/`medium`/`high`. Drift between confidence and `questions_remaining` (e.g. `high` with three load-bearing questions still open) is a smell the judge picks up as a `wrong-reasoning` warning. |
| `next_action` | **Unjustified branch switches** — abandoning a thread without saying why | One concrete step, justified against `learned`. Not a plan tree. Forces causal link between current state and next move into the open. |

The complement of "what reflect prevents" is "when reflect adds
nothing":

- Single-step tasks (one tool call → answer). No state to bank.
- Mechanically obvious sequences (parse response → fill template
  → submit). The next step is implied by the previous result;
  reflect would be narration.
- Pure logging ("I'm about to call X, then Y, then Z"). The
  value is in *state-banking*, not commentary.

Production agents that bank state (reviewer, investigator,
dispatcher in /ask mode) mount the `reflect` skill
(`orchestra/skills/reflect.md`) which bundles the tool with this
contract. Agents whose tasks are single-step responders don't.

#### Skill vs builtin — why reflect lives in the skill layer

`reflect` is registered through the skill layer rather than the
default `register_builtins` chain so that agents whose tasks
don't need state-banking don't carry the cognitive overhead and
schema validation cost. The split keeps the surface honest: an
agent that lists `reflect` in `tools:` (directly or via `skills:
[reflect]`) is **opting in** to the convergence-aid contract,
and the prompt-side guidance that comes with the skill is
visible alongside the tool registration rather than implicit in
the framework's default chain.

The reflect-cadence pusher (`ReflectCadencePusher` in
`orchestra/budget.py`) is wired generically — it only nudges
when `reflect` is in the agent's tool surface, so non-reflective
agents see no cadence pressure. Same for `FailedReflectGuard`
(soft-opt): nothing to guard if reflect isn't there.

### `orchestra/skills.py` + `orchestra/skills/` — composable bundles

Skills are the framework's mechanism for grouping a set of tools
with the rationale / contract / cadence configuration for using
them, and shipping that bundle as a single unit across multiple
agents. A skill is one `.md` file with YAML frontmatter and a
body:

```yaml
---
description: >-
  Short one-paragraph summary — what this skill exists to provide.
tools:
  - diff_list_files          # tools the skill brings into the
  - diff_read_file           # agent's effective tool surface.
extra_tools: [...]           # capture-style tools (less common).
reflect:                     # optional per-area overrides; merged
  with_state: true           # into config.reflect via setdefault.
---
Body markdown — the methodology / contract / when-to-call
guidance. Renders into the agent's user message via the
`{{ skills }}` placeholder.
```

**Mount mechanism.** Both `<agent>.system.md` and the per-call
user message can carry a `skills:` list. `Agent.__init__` unions
the two lists with dedup (system level first, user level
appended), then calls `mount_skills()` which:

1. Loads each skill via `load_skill(name)`.
2. Extends `_fm_meta["tools"]` with the skill's tools.
3. Merges `_fm_meta["reflect"]` (and other per-area dicts) via
   `setdefault` — prompt-declared keys win over skill defaults.
4. Concatenates each skill body with a `## Skill: <name>`
   header and stashes the combined string on
   `self._mounted_skills_body`.

**Render — framework-level injection.** The combined skill body
is injected as a SEPARATE system-role message between the
agent's own system prompt and the conversation / user task —
done by `_build_messages()` in `orchestra/agent.py`. No
per-prompt placeholder is required; user.md files stay clean.
Modern OpenAI-compatible providers (DeepSeek, OpenAI,
Anthropic-via-proxy, …) accept multiple system messages at the
head of the conversation, and the explicit separation makes it
obvious in traces where each surface comes from. An agent with
no `skills:` declared keeps the same single-system-message
shape as before.

Backward-compat: legacy `{{ skills }}` placeholders in existing
user.md files render as empty (`RunContext.skills_body` is now
empty by design) — no double-render. New prompts should NOT
include the placeholder.

`AgentConfig.skills: list[str]` (added 2026-05) carries the
system-level list; `_fm_meta["skills"]` carries the user-level
list. The two lists union at mount time. System-level skills
declare "this agent always wants this" (e.g. investigator always
wants `reflect`); user-level skills are per-task additions.

**Why split into skills rather than inline in system.md.** A
skill is a single source of truth for the tools+methodology
pair. Updating the diff-view methodology happens in
`orchestra/skills/diff_view.md` once, not in three system.md
files. Skills are also discoverable as a unit (an `agent_list_
skills()` discovery tool can enumerate them; agents can pick
which to mount dynamically).

**Current skills.** See `orchestra/skills/`:

| Skill | Tools | Body content |
|---|---|---|
| `reflect` | `reflect` | Per-field contract + cadence default `interval: 5` |
| `prefer_delegation` | `agent_spawn`, `agent_list` | Depth-as-upgrade rationale + `reflect.with_state: true` |
| `diff_view` | `diff_*` (4 tools) | Unified-diff view methodology — ref forms, L/old/new coordinates, posting on `new` |
| `pr_threads` | `pr_list_threads`, `pr_read_thread`, `pr_read_comment` | Look-only-when-relevant dedup rules, snapshot-at-run-start semantic |
| `project_conventions` | — (pure prose) | AGENTS.md / CONVENTIONS.md lookup pattern |
| `finding_format` | — (pure prose) | Finding-dict shape + severity rubric |

**Wiring gotcha.** `Agent.__init__` initially gated SGR-tracker
creation on `reflect in config.tools` (base list only). Skill-
mounted reflect lives in `_fm_meta["tools"]` instead — the gate
missed it, leaving `self.sgr = None` and silently neutering
`ReflectCadencePusher`. Fix: gate on the post-skill-merge
EFFECTIVE tool set (the union). Tests/test_skills.py pins this
to prevent regression.

### `orchestra/budget.py` — pushers + cadence

Pushers are **step-level controllers** (not "LLM handlers" — the
LLM call itself is a single fixed code path in
`orchestra/streaming.py`). They run twice per ReAct iteration,
sandwiching the LLM call + dispatch.

#### StepContext: the per-step middleware ctx

One `StepContext` object lives for the duration of each step. It
carries:

- `state` — the agent's `BudgetState` (tokens used, time elapsed,
  step number).
- `messages` / `all_tools` / `current_tools` — mutable IO the
  pushers may mutate to influence the upcoming LLM call.
- `actions` — producer→consumer queue (see below).
- `step_outcomes` — empty in phase 1, populated in phase 2 with
  `(tool_name, is_error)` per tool call this step ran.
- `event_bus`, `agent_id`, `agent_name` — telemetry attribution.

#### Two phases per step

```
        ┌─── phase 1: apply(ctx) ─────────────┐
        │   tracker.apply_handlers(ctx)        │  pre-LLM
        │   ↓                                  │
        │   handlers append to ctx.actions     │
        │   consumers translate actions →      │
        │     messages / current_tools         │
        │     mutations + telemetry            │
        │   ↓                                  │
        │   ctx.messages / ctx.current_tools   │
        │   are final for the LLM call         │
        └──────────────────────────────────────┘
                      ↓
                  LLM call
                      ↓
              tool dispatch (N tools)
                      ↓
        ┌─── phase 2: on_step_done(ctx) ──────┐
        │   ctx.step_outcomes filled in       │
        │   tracker.notify_step_done(ctx)     │  post-dispatch
        │   ↓                                  │
        │   stateful handlers (counters)       │
        │   inspect outcomes, update state    │
        └──────────────────────────────────────┘
```

#### Producer / consumer split

Pushers don't touch `messages` or `current_tools` directly.
**Producers** append `PusherAction` records describing intent:

```python
PusherAction(type=PusherType.NUDGE,    message="...", kind="sgr")
PusherAction(type=PusherType.FORCE_REFLECT)
PusherAction(type=PusherType.FORCE_DONE)
PusherAction(type=PusherType.CUSTOM,   custom_handler=fn)
```

A single **consumer** stage at the end of phase 1 translates
actions into:

- `NUDGE` → append a `role=user` message with the configured text.
- `FORCE_DONE` → narrow `current_tools` to just `[done]`.
- `FORCE_REFLECT` → **no-op** (intentionally — see
  `ApplyActionsHandler` docstring). The enum value and action are
  preserved for backward compatibility and telemetry, but the tool
  surface is never reduced to reflect-only — that pattern dead-ended
  the agent whenever reflect itself failed validation.
- `CUSTOM` → call the dotted-path handler.

This keeps producers stateless about how their intent surfaces;
they just describe "the agent's token budget is at 75%" without
knowing whether that means a message append or a tool narrowing.

#### Built-in pushers — gradation table

Every dimension that has a hard cap follows a three-level
escalation: **NUDGE @ 0.5 → NUDGE_HIGH @ 0.75 → FORCE_DONE @ end**.
NUDGE_HIGH is the mandatory warning before tool surface narrows —
closes the gap between "halfway" and the terminal cap so the
model has explicit notice that FORCE_DONE is imminent.

| Pusher | Axis (`state.<attr>`) | NUDGE | NUDGE_HIGH | FORCE_DONE | Hard cap → loop exit |
|---|---|:-:|:-:|:-:|---|
| **TokenBudgetPusher** | `token_ratio` = `cumulative_paid / max_tokens` | 0.5 | 0.75 | 1.0 | yes — `max_ratio ≥ 1.0` → `state.exhausted` → break |
| **TimeBudgetPusher** | `wall_ratio` = `elapsed / max_wall_time` | 0.5 | 0.75 | 1.0 | yes, same path. No-op without `max_wall_time`. |
| **StepBudgetPusher** | `step_ratio` = `steps_used / max_steps` | 0.5 | 0.75 | **0.90** ⚡ | yes via `step ≥ max_steps`. FORCE_DONE deliberately fires earlier (10% headroom) so done() actually has room to run. |
| **ContextBudgetPusher** | `context_ratio` = `tokens_in / max_context` | 0.5 | 0.75 | (none) | yes — `context_ratio` participates in `max_ratio`, so 1.0 still triggers `state.exhausted` (see gotcha below) |
| **ReflectCadencePusher** | counter `steps_since_reflect` | at `reflect_interval` | — | — | — |
| **RatioPusher** | `max_ratio` (max across all axes) | YAML-config | YAML-config | YAML-config | — |

**Action → tool / message effect:**

| Action | What `ApplyActionsHandler` does | Visibility on next LLM call |
|---|---|---|
| `NUDGE` | append `{role: "user", content: msg}` to `ctx.messages` | shows up as a user-message |
| `FORCE_DONE` | narrow `ctx.current_tools` to `[done]` if `done` is in the surface + append message | next call's tools schema has only `done` |
| `FORCE_REFLECT` | **no-op** (action recorded for telemetry; nothing else) | — |
| `CUSTOM` | invoke the action's `custom_handler(messages, state)` | depends on the handler |

**Soft-opt / specialised pushers (NOT in default chain):**

| Pusher | When | Effect |
|---|---|---|
| `FailedReflectGuard` | N consecutive failed reflects (default 3) | Latches → hides `reflect` from `current_tools` + injects an explanation. One-shot per run. |
| `RatioEscalationPusher` `force_reflect_at` opt-in | YAML-configured | Adds a FORCE_REFLECT level — currently a no-op action, kept for telemetry only. |

#### Two distinct termination mechanisms

Often conflated:

1. **FORCE_DONE action** (soft). `ApplyActionsHandler` narrows
   `ctx.current_tools` to `[done]` for the **next** LLM call.
   The agent still runs that call, emits `done()`, and exits
   cleanly. Step axis sets FORCE_DONE @ 0.90 specifically so the
   agent has headroom to do this before the loop's own hard cap.

2. **Loop exhaustion** (hard). `state.exhausted` (max of all axis
   ratios ≥ 1.0) or `step ≥ max_steps` → the for-loop in `Agent.run`
   breaks. `_force_done` (`orchestra/agent.py:_force_done`) makes
   one final LLM call narrowed to `done` to extract findings as a
   recovery — but this can fail.

Ideal: the FORCE_DONE action fires **before** loop exhaustion and
done() runs in the normal flow. Step axis demonstrates this
explicitly (0.90 < 1.0). Token / time FORCE_DONE @ 1.0 race with
exhaustion and may not always succeed cleanly.

#### Gotcha — context axis is "NUDGE-only by design" but participates in `max_ratio`

`ContextBudgetPusher` deliberately has no FORCE_DONE level (per the
"report state, don't dictate" convention above) — different agents
have different remedies for a full context (spawn, condense, wrap
up), so the pusher just reports state.

But `context_ratio` is included in `BudgetState.max_ratio`, and
`state.exhausted` is `max_ratio ≥ 1.0`. So if `tokens_in` actually
hits `max_context`, the loop still breaks via exhaustion — bypassing
the pusher's "NUDGE-only" intent. The two design options for
resolving this are:

- (a) Exclude `context_ratio` from `max_ratio` — context becomes a
  pure monitor signal, never terminates the loop.
- (b) Accept that 1.0 is a physical hard cap (the model literally
  can't take more) and document it as such — current behaviour.

Currently (b) by default, but the conflict between "no FORCE_DONE
on context axis" and "context kills the loop at 1.0" is real and
worth surfacing here.

#### Configured via prompt frontmatter

```yaml
budget:
  max_tokens: 50000
  max_steps: 40
  max_wall_time: 600
  max_context: 128000
  pushers:                       # extras layered on top of defaults
    - { at: 0.7, type: nudge,      message: "70% tokens used — start consolidating." }
    - { at: 1.0, type: force_done }
```

**Effective `max_context` precedence** (lowest → highest):
default 128000 → provider profile → `config.yaml review.max_context`
→ base-prompt `budget.max_context` frontmatter (compiler) →
per-run user-message override `budget.max_context` frontmatter
(Agent.__init__). The override-frontmatter wins — that's what
unit-tier scenarios like REV-U-008 use to force context pressure
(`budget: { max_context: 16000 }` inside
`diffgraph/test_prompts/reviewer/budget-aware-delegation.md`),
keeping "everything that shapes the agent for this scenario lives
in the prompt the agent reads" as the rule. Same channel covers
`max_tokens` / `max_steps` / `max_wall_time` — only listed fields
get overridden; unspecified ones inherit the base. Implementation
lives in `orchestra/agent.py::__init__` (merge block) and
`diffgraph/orchestrator.py::run_agent` (programmatic API path —
still respects the `max_context=` kwarg for library callers).

The `BudgetTracker` (`orchestra/budget.py::BudgetTracker`)
assembles the handler list from this config plus the framework's
built-in chain (ReflectCadenceCounter / RatioPusher /
TokenBudgetPusher / TimeBudgetPusher / StepBudgetPusher /
ContextBudgetPusher / ApplyActionsHandler / TracingHandler), and
exposes `apply_handlers(ctx)` / `notify_step_done(ctx)` to the
agent loop.

### Three kinds of "handler" in the framework

This often confuses readers — there are **three** distinct places
the word "handler" shows up. None of them are interchangeable.

| Kind                | Where                              | When it runs                                          | What it returns                          |
|---------------------|------------------------------------|-------------------------------------------------------|------------------------------------------|
| **`PusherHandler`** | `orchestra/budget.py`              | per ReAct step (phase 1 pre-LLM, phase 2 post-dispatch) | nothing; mutates `ctx` via `actions`     |
| **`ToolDef.handler`** | `orchestra/tools/registry.py`     | once per accepted tool call (after schema validation) | the tool's result (str/dict)             |
| **`ArgRepairHandler`** | `orchestra/tools/arg_repair.py`  | on validation failure with missing-required error      | repaired `args` dict, or `None` to defer |

In particular:

- **No "LLM handler" plug-point.** The LLM call itself
  (`orchestra/streaming.py::llm_call_streaming`) is fixed code.
  Vendor-specific knobs (streaming on/off, `extra_body`,
  `tool_choice`) are passed as parameters, not via a handler chain.
  If you need to intercept the LLM round-trip, do it before
  (via a pusher mutating `ctx.messages` / `ctx.current_tools`) or
  after (via tool-result formatting or an event subscriber).

- **EventBus subscribers** (`TraceCollector`, `TraceDBWriter`,
  `FSSpanExporter`) are observers, not handlers — they record
  what happened but can't change behaviour. Same applies to the
  OTel span emitters.

- **Tool handler vs ArgRepairHandler** is the cleanest split:
  ArgRepair operates on the **arguments** before they reach the
  tool handler, only when validation flagged a missing field.
  The tool handler itself sees a dict that already passed schema
  validation — it never has to defend against malformed JSON or
  missing required fields.

### `orchestra/condensation.py` — history compaction

When `usage.total_tokens > condensation.trigger`, the framework
replaces the middle of `messages` with an LLM-generated summary,
preserving `preserve_last` recent messages and (optionally) every
`reflect` if `preserve_sgr=True`. The condense LLM call uses the
same client as the main loop.

### `orchestra/streaming.py` — LLM I/O

Thin wrapper around the OpenAI client. Handles:

- Streaming vs non-streaming (`stream` from `llm_params`).
- `tool_choice` (`required` / `auto`).
- `extra_body` forwarding (vendor-specific knobs like
  `chat_template_kwargs.enable_thinking`).
- Token accounting via `usage` field reconstruction when streaming.

### `orchestra/events.py` + `orchestra/trace.py` — observability

`EventBus` is a simple sync emitter. Subscribed by:

- `TraceCollector` — builds an in-memory tree that mirrors agent
  spawn relationships and per-step request/response/SGR.
- `TraceDBWriter` (`orchestra/trace_db.py`) — appends to the QA
  SQLite trace DB (`runs` + `events` + `otel_spans` tables).
- `FSSpanExporter` (`orchestra/otel_fs.py`) — drops per-span
  payloads as files for the offline viewer.

`orchestra/trace.py:_prepare_agent` is what the QA UI consumes via
`/api/runs/{id}/json` — it pairs each step's request and response,
walks children recursively, and tags health flags
(`tool_errors_count`, `repeats_prev_step`).

### `orchestra/otel.py` + `otel_fs.py` — OpenTelemetry

`setup_tracing(...)` wires up `OTLPSpanExporter` (HTTP) and the
filesystem exporter. `set_domain_attrs(...)` stamps the active
context with `diffgraph.run_id`, `scenario_id`, `mutation`,
`plan_id`, `task_id`, `lineage`, `agent_name`, etc., so every
span — `agent.<name>`, `llm.request`, `tool.<name>` — is
self-describing without a JOIN against `runs`.

`observe(name, attributes=...)` is the single span-context wrapper
LLM and tool calls flow through. Payloads land in two places:
attributes (compact dims) and stash-files (full request/response
bodies + tool args/results).

### `orchestra/bench_log.py` — per-task unified system log

Sister to OTel — same correlation IDs, different consumers. Where
OTel spans answer "what did the agent DO at each step", bench-log
answers "what did every subsystem TELL us along the way" — git
output, judge stdout, worker lifecycle, scheduler decisions, …
all collapsed into ONE timestamped JSON-lines stream per QA task.

Layout (`~/.diffgraph/bench-logs/task-{id}/`):

| File          | Producer                          | Contents                                       |
|---------------|-----------------------------------|------------------------------------------------|
| `stdout.log`  | bench subprocess (FD-written)     | non-Python output (shell, git, echo'd lines)   |
| `stderr.log`  | bench subprocess (FD-written)     | non-Python error output (git's stderr too)     |
| `system.log`  | Python `logging.*` via handler    | JSON lines from worker / bench / cli / judge   |
| `meta.json`   | worker, written pre + post fork   | task / run / plan / scenario IDs, cmd, exit_code, timestamps |

Each subsystem opts in by calling
`setup_bench_logging(system=<name>)` at startup. The function:
- reads `DIFFGRAPH_TASK_ID` from env if `task_id=` not passed
- returns `None` (no-op) when no task scope — ad-hoc CLI runs
  don't leak log files into the user's home
- is idempotent across re-installs so subprocesses can refresh
  correlation IDs without stacking handlers
- captures `exc_info` as a separate `traceback` field plus a
  one-line `exc` summary
- surfaces caller's `extra={...}` kwargs as top-level JSON keys

Wired into: `quality_cli/main.py` (worker, `system="worker"`),
`cli.py` (diff-graph, `system="diffgraph"`), `benchmark/cli.py`
(`system="bench"`). Plumbing via env:
`DIFFGRAPH_BENCH_LOGS_DIR`, `DIFFGRAPH_TASK_ID`,
`DIFFGRAPH_TRACE_RUN_ID`, `DIFFGRAPH_PLAN_ID`,
`DIFFGRAPH_SCENARIO_ID`.

Consumers:
- `GET /api/qa/tasks/{id}/bench-log?stream=combined|stdout|stderr|system&as=text|json`
- `GET /api/runs/{run_id}/bench-log` — alias via `qa_tasks.trace_run_id`
- Trace UI: 📜 button in `/qa/sessions/{run_id}` header opens a
  right-pane tab with the combined view
- Plans UI: `📜` link on every task chip in the expand-plan
  detail strip — works even when the agent never started (bench
  crashed at setup → no `runs` row, but bench-log dir still
  exists because the worker creates it BEFORE forking).
- `quality-cli traces bench-log <run_id|task_id>` — pipe-friendly

The bench-log feature is **independent** from OTel — they share
correlation IDs but write to separate stores. The trace tree
answers "what the agent did"; the bench-log answers "why didn't
the agent even start", which is the failure mode OTel can't
observe because there are no spans for it. See plan 212 task
#3636 (git clone exit 128 with reason buried in
`CalledProcessError.stderr`) for the wild-type case the layer
exists to solve.

### `orchestra/tool_mocks.py` — test fixture mocks

Mockito-style ordinal mocks. A benchmark scenario can declare:

```yaml
mocks:
  - tool: pr_post_comment
    when: { args.severity: "MAJOR" }
    return: { status: "posted", comment_id: 42 }
```

`Agent.dispatch_tool` intercepts the call BEFORE registry dispatch
if `tool_mocks.has(name)` is True. Tests get deterministic tool
results without spinning up the real handler. Mismatched fixture
args raise `MockArgsMismatchError`; exhausted slots raise
`MockExhaustedError` — both surface as test failures, not silent
drift.

**`{mode: capture_only}` preset** (delegation-isolation pattern).
For scenarios that assert on *whether* an agent spawned children
(via the `intended_spawns` judge channel) but don't care what the
child actually returns, the fixture can short-circuit
`agent_spawn` without standing up child stubs:

```yaml
mocks:
  - tool: agent_spawn
    return: { mode: capture_only }
```

The mock returns `{"status":"spawned","child_id":"<test-stub>",
"mode":"capture_only"}` for every call; the spawn event still hits
the trace (so the judge sees it), but no child run is created.
Used by REV-U-008 (budget-aware delegation) — see
`benchmarks/runner/judge.py::_load_intended_spawns`.

### `orchestra/handoff.py` + `orchestra/feedback.py`

Inter-agent state. When a parent agent calls
`agent_spawn("child", focus=...)`, the framework builds the child's
prompt with (a) the static system prompt, (b) the user-message
template with placeholders resolved from `data`, (c) optional
handoff context (last reflect, full reflect history, …) per the
prompt's `handoff:` field, (d) feedback messages from prior failed
attempts in this run.

## 3. Domain layering

`orchestra/` knows nothing about Bitbucket, diffs, or PR reviews.
The diff-graph stack plugs in like this:

- `diffgraph/orchestra_tools.py` — registers domain tools
  (`diff_*`, `pr_post_comment`, `set_review_status`, comment-graph
  tools, etc.) on a per-call `ToolRegistry`.
- `diffgraph/prompts/` — the prompt directory passed to
  `compile_prompts`. Defines `dispatcher`, `reviewer`,
  `investigator`, `judge.raw`.
- `diffgraph/orchestrator.py` — the `run_review(...)` entry point
  that builds the context, registers the tools, and calls
  `run_agent("reviewer")`. The library API
  (`diffgraph.api.DiffGraph`) is the public-facing wrapper.
- `cli.py` — the binary surface for one-shot review runs and
  webhook-style replays.

`quality_api/` and `quality_cli/` use the same primitives for the
QA bench: scheduling, multi-tenant runs, scoring, etc.

## 4. Argument repair chain (the qwen3 fixes)

Tool-call arguments arrive from the LLM as a JSON-encoded STRING
in `tc.function.arguments`. Some models (qwen3-coder on vLLM /
modelrun) periodically emit malformed JSON or mis-shaped objects.
The dispatch path runs schema validation; on failure, it walks a
chain of `ArgRepairHandler` instances **only when** the error is
a missing-required-property failure. This gating is enforced by
the harness (`ToolRegistry.dispatch`), not by individual handlers,
so a custom handler can't accidentally widen the trigger.

The default qwen3 chain (`fix_qwen3_stringification_bug=True`):

1. **`TruncatedJsonHandler`** — operates on the RAW arguments
   string. Fires when `args == {}` (parse fell back) and the raw
   string contains a recoverable `"key": ,` / `"key": }` empty
   pair. Drops the truncated key, re-parses, returns the dict.

   Observed wild-type: plan 192 reviewer step 10 emitted 6
   `pr_post_comment` calls all ending in `"parent_id": }`. The regex
   recovers `text`, `file`, `line`, `severity` intact.

2. **`StringifiedArgsHandler`** — operates on the already-parsed
   dict. When one string property contains nested escaped-JSON
   whose keys cover the missing-required set, lifts them to the
   top level.

   Observed wild-type: qwen3 reflect emitting
   `{"learned": "...\"confidence\": \"high\"..."}` instead of
   distinct top-level keys.

Every handler proposal is **re-validated** against the same schema
before being committed. A proposal that doesn't fix the validation
error is rejected; the next handler is invoked with the ORIGINAL
args (failed candidates don't poison the chain). A handler that
raises is logged and skipped.

Caller registers a custom chain via:

```python
ToolRegistry(arg_repair_handlers=[MyHandler(), ...])
```

or enables the default qwen3 chain via:

```python
ToolRegistry(fix_qwen3_stringification_bug=True)
```

Provider profiles (`.llm_creds.toml`) carry the flag per-model so
non-qwen3 paths pay zero cost (chain is empty → fast path stays
the inline validate-and-call).

## 5. Where to look next

| Q                                              | File                                       |
|------------------------------------------------|--------------------------------------------|
| How does an agent assemble its messages?       | `orchestra/agent.py::_build_messages`      |
| What does a `tools:` frontmatter entry mean?   | `orchestra/prompts/frontmatter.py`         |
| When does reflect-nudge fire?                  | `orchestra/budget.py::ReflectCadenceCounter` |
| How is `tc.function.arguments` parsed?         | `orchestra/agent.py:990` (line ~990)       |
| Where do spans get their domain attrs?         | `orchestra/otel.py::set_domain_attrs`      |
| How do tool mocks intercept?                   | `orchestra/agent.py::dispatch_tool` + `tool_mocks.py` |
| What goes into the QA trace DB?                | `orchestra/trace_db.py`                    |
| How do I add a new agent prompt?               | drop a `.md` in the prompt dir, set `tools:` in frontmatter, register any new tools |
| How do I add a new tool?                       | `registry.register(name=..., parameters=...)` + add the name to the prompt's `tools:` |
| How do I add a new arg-repair handler?         | implement `ArgRepairHandler.try_repair` + pass via `arg_repair_handlers=` |
| How do I add a new pusher?                     | implement `PusherHandler.apply` (and optionally `on_step_done`), append to `BudgetTracker.handlers` |
| What's the difference between a pusher and a tool handler? | pusher = per-step controller (mutates `ctx`); tool handler = per-call callable (returns a value). See §"Three kinds of handler". |
