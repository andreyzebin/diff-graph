# DiffGraph

Multi-agent PR code reviewer powered by the **Orchestra** orchestration framework. Takes a git diff (or a Bitbucket Server PR URL) and runs a configurable agent topology: a Strategist agent plans what to look for, then one or more ReAct Solver agents explore the repo and produce structured inline findings.

```
git diff / PR URL
      │
      ▼
parse_diff()       changed files + changed lines
      │
      ▼
┌─────────────────────────── Orchestra Topology ───────────────────────────┐
│                                                                          │
│  strategist (single)    one LLM call → typed task list                   │
│       │                                                                  │
│       ▼                                                                  │
│  reviewer (react)       ReAct loop: tools, SGR reflect, budget pushers   │
│       │                 can spawn sub-agents / fork branches             │
│       ▼                                                                  │
│  [optional: fan_out → N parallel agents → join/synthesizer]              │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
      │
      ▼
ReviewFinding[]    BLOCKER / MAJOR / MINOR / COMMENT with evidence
      │
      └──► post inline comments to PR
```

No pre-indexing. No database. One session per diff.

---

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Set up credentials:

```bash
cp .env.example .env
# edit .env — fill in API keys
source .env

cp config.yaml config.local.yaml
# edit config.local.yaml — set api_url and model if not using OpenAI
```

Run against a local diff:

```bash
git diff HEAD~1 | python cli.py run --repo . --diff -
```

Run against a Bitbucket Server PR:

```bash
source .env
python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42
```

Post findings as inline PR comments:

```bash
python cli.py run --pr-url ... --post-comments
```

---

## Configuration

### `.env`

Secrets and environment-specific values. Copy from `.env.example`, fill in, and `source .env` before running.

```bash
# LLM
OPENAI_API_KEY=sk-...
# or for other providers:
DEEPSEEK_API_KEY=sk-...

# Bitbucket Server
BITBUCKET_SERVER_BEARER_TOKEN=...
REQUESTS_CA_BUNDLE=/path/to/ca.pem        # optional, custom CA
BITBUCKET_SERVER_CLIENT_CERT=/path/to/client.pem  # optional, mTLS
```

### `config.local.yaml`

Runtime settings. Deep-merged on top of `config.yaml`. Gitignored, never committed.

```yaml
llm:
  api_url: "https://api.deepseek.com/v1"   # empty = OpenAI directly
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"

review:
  max_steps: 40        # max ReAct tool calls per review
  max_tokens: 40000    # token budget for the solve phase
```

---

## CLI

```bash
# Local diff
python cli.py run --repo ./my-service --diff changes.diff
git diff HEAD~1 | python cli.py run --repo . --diff -

# Bitbucket Server PR
python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42

# Post findings as inline PR comments
python cli.py run --pr-url ... --post-comments

# Write findings as JSON
python cli.py run --repo . --diff my.diff --output findings.json

# Parse only — no LLM
python cli.py inspect changes.diff
git diff HEAD~1 | python cli.py inspect -
```

**`run` flags:**

| Flag | Description |
|------|-------------|
| `--repo` / `-r` | Path to repository |
| `--diff` / `-d` | Diff file path, or `-` for stdin |
| `--pr-url` | Bitbucket Server PR URL — clones repo and fetches diff automatically |
| `--post-comments` | Post findings to the PR as inline comments (requires `--pr-url`) |
| `--model` / `-m` | LLM model override |
| `--api-url` | OpenAI-compatible API base URL override |
| `--api-key` | API key override |
| `--output` / `-o` | Write findings as JSON to file |
| `--max-steps` | Max ReAct tool calls (default: from config) |
| `--max-tokens` | Max token budget (default: from config) |

---

## Python API

```python
from openai import OpenAI
from diffgraph import DiffGraph

client = OpenAI()
dg = DiffGraph(repo_path="./my-service", llm_client=client)

findings, review_ctx = dg.review(open("my.diff").read())

for f in findings:
    print(f.severity, f.file, f.line, f.title)
    print(f.explanation)
    if f.suggestion:
        print("Fix:", f.suggestion)

# With progress callback
def on_event(event, **kw):
    if event == "orchestrator_plan_done":
        print("plan:", kw["plan"]["system_type"])
    elif event == "orchestrator_step":
        print(f"  step {kw['step']}  {kw['tool']}")
    elif event == "orchestrator_reflect":
        print(f"  reflect [{kw['confidence']}]: {kw['next_action']}")

findings, _ = dg.review(diff_text, on_event=on_event)

# Incremental review — pass existing PR comments
findings, ctx = dg.review(diff_text, existing_comments=[
    {"id": 42, "file": "src/Foo.java", "line": 10, "text": "...", "resolved": False}
])
# ctx.comment_replies  — threads the agent wants to reply to
# ctx.comment_resolves — thread IDs the agent considers resolved
```

---

## Orchestra Framework

DiffGraph is built on **Orchestra**, a general-purpose multi-agent orchestration framework that lives in the `orchestra/` directory. Orchestra can be used independently of DiffGraph for any multi-agent workflow.

### Key capabilities

| Capability | Description |
|---|---|
| **YAML-configurable agents** | Define agents with system prompts, tools, SGR, budgets, and adaptive params in YAML or Python |
| **DAG topologies** | Wire agents into directed acyclic graphs with `single`, `react`, `fan_out`, and `join` node types |
| **Context handoff** | 7 built-in modes: `full_history`, `sgr_outcomes`, `all_sgr`, `findings_only`, `findings_and_sgr`, `condensed`, `last_N` |
| **Dynamic spawning** | Agents spawn sub-agents via tool call, JSON output, or SGR auto-fork |
| **SGR (Self-Guided Reasoning)** | Structured `reflect()` with question tracking, confidence, custom extension fields |
| **Budget pushers** | Configurable `(threshold, action)` pairs: `nudge`, `force_reflect`, `force_done`, `custom` |
| **Message condensation** | 4 strategies for long-running agents: `llm_summary`, `sliding_window`, `drop_tool_results`, `hybrid` |
| **Shared/swarm tools** | Thread-safe `AppendLog`, `MutexMap`, `Blackboard` for agent coordination |
| **Adaptive LLM params** | Temperature, penalties, model switch per-step driven by budget, SGR confidence, repetition score |
| **Behavioral feedback** | Repetition detector, progress tracker, stuck detector with configurable actions |
| **Cross-agent feedback loops** | Topology-level observer→target wiring to adjust child agent params/budget in real time |
| **Fork & merge** | Agents fork into parallel branches; merge via `best_confidence`, `union`, `llm_merge`, or custom |
| **Observability** | 22 event types, full execution tree reconstruction, per-agent token accounting |

### Using Orchestra directly

```python
from orchestra import (
    Agent, AgentConfig, BudgetConfig, ToolRegistry,
    Topology, TopologyRunner, EventBus, OrchestraConfig,
    NodeConfig, EdgeConfig, TopologyConfig, NodeType,
    PusherConfig, PusherType,
)

# 1. Define agents
config = OrchestraConfig(
    agents={
        "planner": AgentConfig(
            name="planner",
            system_prompt="You plan research tasks. Output JSON with a 'tasks' array.",
            sgr=False,
            budget=BudgetConfig(max_steps=1),
        ),
        "researcher": AgentConfig(
            name="researcher",
            system_prompt="You research topics using available tools.",
            sgr=True,
            tools=["search", "read_file"],
            budget=BudgetConfig(
                max_tokens=20000, max_steps=20,
                pushers=[
                    PusherConfig(at=0.75, type=PusherType.NUDGE, message="Wrap up soon."),
                    PusherConfig(at=1.0, type=PusherType.FORCE_DONE),
                ],
            ),
        ),
    },
    topologies={
        "research": TopologyConfig(
            name="research",
            nodes=[
                NodeConfig(id="plan", agent="planner", type=NodeType.SINGLE),
                NodeConfig(id="research", agent="researcher", type=NodeType.REACT),
            ],
            edges=[
                EdgeConfig(from_node="plan", to_node="research", context_handoff="findings_only"),
            ],
        ),
    },
)

# 2. Register tools
registry = ToolRegistry()

@registry.register
def search(query: str) -> str:
    """Search for information."""
    return f"Results for: {query}"

# 3. Run topology
from openai import OpenAI
llm = OpenAI()
event_bus = EventBus()

topology = Topology(config.topologies["research"])
runner = TopologyRunner(topology, config, registry, llm, "gpt-4o-mini", event_bus)
result = runner.run()

print(result.final_output)
```

### Fan-out topology (parallel agents)

```python
# Strategist outputs tasks → N parallel reviewers → synthesizer joins results
topologies:
  parallel_review:
    nodes:
      - id: strategist
        agent: strategist
        type: single
      - id: reviewers
        agent: reviewer
        type: fan_out
        source: strategist
        parallel: true
        spawn_from: tasks       # one agent per task in strategist output
      - id: synthesizer
        agent: synthesizer
        type: join
        sources: [reviewers]
    edges:
      - from: strategist
        to: reviewers
        context_handoff: findings_only
      - from: reviewers
        to: synthesizer
        context_handoff: findings_and_sgr
```

### Adaptive LLM parameters

```yaml
agents:
  deep_researcher:
    llm_params:
      temperature: 0.3
      schedules:
        # Lower temperature as budget depletes
        - param: temperature
          driver: budget_ratio
          curve: linear_decay
          range: [0.4, 0.1]
        # Increase frequency penalty when stuck in loops
        - param: frequency_penalty
          driver: repetition_score
          curve: linear
          range: [0.0, 1.5]
```

See [REQUIREMENTS.md](REQUIREMENTS.md) for the full technical specification (68 requirements across 11 sections).

---

## How it works

### Plan phase

One non-streaming LLM call. The strategist receives a compact diff summary and outputs a JSON plan:

```json
{
  "system_type": "spring-service",
  "tasks": [
    {
      "id": "check_callers",
      "type": "call_chain",
      "priority": "high",
      "focus": "Find all callers of PaymentService.processPayment()",
      "search_hints": ["processPayment", "PaymentService"]
    }
  ]
}
```

Task types: `call_chain`, `security_config`, `data_model`, `error_handling`, `concurrency`, `business_logic`, `code_conventions`.

### Solve phase

ReAct loop up to `max_steps`. Tools:

| Tool | Description |
|------|-------------|
| `find_files(pattern)` | Glob the repo |
| `read_file(path, start?, end?)` | Read up to 100 lines |
| `read_outline(path)` | Structural outline via tree-sitter (classes, methods, line ranges) |
| `search(query, glob?, regex?)` | Text search across files |
| `get_diff(path?)` | Full diff or per-file section |
| `reply_to_comment(id, text)` | Queue a reply to an existing comment thread |
| `resolve_comment(id)` | Queue a resolve on an existing comment thread |
| `reflect(learned, resolved_questions, questions_remaining, confidence, next_action)` | SGR structured self-reflection |
| `done(findings)` | Submit findings and stop |

Multiple tool calls from a single LLM response execute in parallel via `ThreadPoolExecutor`.

Adaptive budget nudges at 50% and 75% of `max_tokens` push the agent toward wrapping up.

### SGR (Self-Guided Reasoning)

The agent calls `reflect()` every 3-5 steps to track what it has learned, what questions remain open, and what to do next.

The `resolved_questions` field requires the agent to explicitly answer or drop every question from the previous reflect — no silent omissions. Each entry carries a `resolution` (`"answered"` / `"dropped"`) and a `summary` (the answer or reason for dropping).

The CLI renders a live convergence panel at the bottom of the terminal showing:
- **Confidence trajectory** — `low → medium → high` across all reflects
- **Open questions** — colour-coded by age: green (new), yellow (1 reflect old), red (stale, 2+)
- **Resolved section** — last 5 answered/dropped questions with their summaries

`reflect()` returns `"Reflection noted."` — its value is in structuring the agent's reasoning and making convergence visible.

### Incremental review

If `existing_comments` is provided, the agent sees all open threads and can:
- `resolve_comment(id)` — when the issue is addressed in the new diff
- `reply_to_comment(id, text)` — when a fix is incomplete or needs follow-up

These actions are queued in `ReviewContext` and applied by the caller after `done()`.

---

## Supported languages

Java · Python · TypeScript / TSX · Go · Kotlin · Ruby · C#

(tree-sitter structural outlines for all; plain line-count fallback for unknown extensions)

---

## Architecture

```
orchestra/                   General-purpose multi-agent framework (4,400 LOC)
├── types.py                 Core dataclasses (AgentConfig, BudgetConfig, TopologyConfig, ...)
├── config.py                YAML loading, env var expansion, validation
├── events.py                EventBus with 22 event types
├── agent.py                 Agent: single-shot + ReAct loop with SGR, budget, condensation
├── topology.py              DAG definition, validation, topological sort
├── runner.py                TopologyRunner: executes DAG, fan_out/join, spawn, fork
├── budget.py                BudgetState, BudgetTracker, child budget partitioning
├── sgr.py                   SGR tracker with extensions + handoff extraction
├── handoff.py               7 built-in context handoff modes + compose
├── condensation.py          4 message condensation strategies
├── streaming.py             LLM streaming with adaptive param passthrough
├── adaptive.py              Schedule drivers, curves, AdaptiveParamResolver
├── feedback.py              Repetition detector, progress tracker, stuck detector
├── feedback_loops.py        Cross-agent feedback loop manager
├── merge.py                 Fork/join merge strategies
├── prompts.py               Template loading + {var} interpolation
└── tools/
    ├── registry.py          @register decorator, YAML tools, OpenAI schema generation
    ├── builtin.py           reflect, done, spawn_agent, fork tool factories
    └── shared.py            AppendLog, MutexMap, Blackboard (thread-safe swarm tools)

diffgraph/                   Code review domain logic
├── api.py                   DiffGraph public API
├── orchestrator.py          Thin wrapper: builds Orchestra topology for code review
├── orchestra_tools.py       Domain tools registered as closures (find_files, read_file, ...)
├── diff_parser.py           git diff text → DiffResult (hunks, changed lines)
├── lang.py                  Language detection
├── tools.py                 Filesystem primitives (list_files, read_file, search_text)
├── outline.py               tree-sitter structural outline
├── bitbucket.py             Bitbucket Server integration (fetch/post/reply/resolve)
└── prompts/
    ├── strategist_system.txt    Plan phase prompt
    └── orchestrator_system.txt  ReAct + SGR prompt
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```
