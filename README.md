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

DiffGraph is built on **Orchestra**, an agent-first orchestration framework (3,200 LOC) that lives in `orchestra/`. No topologies, no pipelines — agents create all structure at runtime via tool calls.

### Design principles

| Principle | Meaning |
|---|---|
| **Agent-first** | Only two modes: `single` (one LLM call) and `react` (non-deterministic tool loop). No topology runner. |
| **Tools, not config** | Spawn, fork, plan, adjust — all via tool calls at runtime, not YAML declarations |
| **Prompts, not pipelines** | Agent behavior is determined by prompts. The framework imposes no workflow. |
| **Mutable params** | LLM params (temperature, penalties, model) are live state. Other agents change them via `adjust_agent`. |
| **Supervisor = agent** | Feedback loops and coordination via a supervisor agent with `adjust_agent` + `observe_agents` tools |

### Meta-tools

| Tool | What it does |
|---|---|
| `spawn_agent` | Create and run a child agent. Sync (wait) or async. |
| `spawn_many` | Fan-out N agents in parallel, return merged results |
| `plan` | Spawn a planner that returns structured JSON tasks |
| `fork` | Clone self into N parallel branches with different focus |
| `adjust_agent` | Change a child agent's temperature, penalties, model, inject message, extend budget |
| `observe_agents` | Get status of all children: step, budget, SGR confidence, last tool |
| `reflect` | SGR self-reflection with question tracking |
| `done` | Submit output and stop |

### Using Orchestra directly

```python
from orchestra import (
    Agent, AgentConfig, AgentMode, BudgetConfig, ToolRegistry,
    EventBus, PusherConfig, PusherType, LLMParamsConfig,
)
from orchestra.tools.builtin import register_builtins
from orchestra.sgr import SGRTracker

# 1. Register domain tools
registry = ToolRegistry()

@registry.register
def search(query: str) -> str:
    """Search for information."""
    return f"Results for: {query}"

# 2. Define agent
config = AgentConfig(
    name="researcher",
    system_prompt="You research topics using tools. Call done() with findings.",
    mode=AgentMode.REACT,
    sgr=True,
    tools=["search"],
    meta_tools=["plan", "spawn_agent"],  # can plan and spawn sub-agents
    budget=BudgetConfig(
        max_tokens=20000, max_steps=20,
        pushers=[
            PusherConfig(at=0.75, type=PusherType.NUDGE, message="Wrap up soon."),
            PusherConfig(at=1.0, type=PusherType.FORCE_DONE),
        ],
    ),
    llm_params=LLMParamsConfig(temperature=0.3),
)

# 3. Register builtins and run
sgr = SGRTracker()
register_builtins(registry, config, sgr_tracker=sgr)

from openai import OpenAI
agent = Agent(
    config=config, tool_registry=registry,
    llm=OpenAI(), model="gpt-4o-mini", event_bus=EventBus(),
)
result = agent.run()
print(result.output)
```

### Supervisor pattern (agent controls other agents' params)

```python
# A supervisor agent with adjust_agent + observe_agents
# All control logic lives in the prompt, not in framework config
supervisor = AgentConfig(
    name="supervisor",
    system_prompt="""You oversee research agents.
Call observe_agents() to check their progress.
If an agent is stuck (low confidence, high repetition):
  - Raise temperature to 0.7 to encourage creativity
  - Set frequency_penalty to 1.5 to break loops
  - Inject a message suggesting a different angle
When all children are done, call done() with consolidated results.""",
    mode=AgentMode.REACT,
    sgr=True,
    meta_tools=["spawn_agent", "spawn_many", "adjust_agent", "observe_agents"],
)
```

See [REQUIREMENTS.md](REQUIREMENTS.md) for the full technical specification.

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
orchestra/                   Agent-first orchestration framework (3,200 LOC)
├── types.py                 Core dataclasses (AgentConfig, BudgetConfig, LLMParamsConfig)
├── config.py                YAML loading, env var expansion, validation
├── events.py                EventBus with typed events
├── agent.py                 Agent: single + react, spawn/fork/plan/adjust built-in
├── budget.py                BudgetState, BudgetTracker, child budget partitioning
├── sgr.py                   SGR tracker with extensions + handoff extraction
├── handoff.py               7 context handoff modes + compose
├── condensation.py          4 message condensation strategies
├── streaming.py             LLM streaming with param passthrough
├── feedback.py              Read-only signals: repetition, progress, stuck detection
├── merge.py                 Fork/join merge strategies
├── prompts.py               Template loading + {var} interpolation
└── tools/
    ├── registry.py          @register decorator, YAML tools, OpenAI schema generation
    ├── builtin.py           reflect, done, spawn_agent, spawn_many, plan, fork,
    │                        adjust_agent, observe_agents — schema definitions
    └── shared.py            AppendLog, MutexMap, Blackboard (thread-safe swarm tools)

diffgraph/                   Code review domain logic
├── api.py                   DiffGraph public API
├── orchestrator.py          Creates strategist + reviewer agents directly
├── orchestra_tools.py       Domain tools registered as closures
├── diff_parser.py           git diff text → DiffResult (hunks, changed lines)
├── lang.py                  Language detection
├── tools.py                 Filesystem primitives (list_files, read_file, search_text)
├── outline.py               tree-sitter structural outline
├── bitbucket.py             Bitbucket Server integration
└── prompts/
    ├── strategist_system.txt    Plan phase prompt
    └── orchestrator_system.txt  ReAct + SGR prompt
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```
