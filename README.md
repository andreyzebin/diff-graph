# DiffGraph

Multi-agent PR code reviewer powered by the **Orchestra** framework. Takes a git diff (or a Bitbucket Server PR URL), spawns a strategist agent that analyzes the change, delegates investigation to focused reviewer agents, and consolidates findings into a deduplicated list.

```
git diff / PR URL
      │
      ▼
parse_diff()         changed files + changed lines
      │
      ▼
┌───────────────────── Orchestra ─────────────────────┐
│                                                      │
│  strategist (react)                                  │
│    Phase 1: ANALYZE — get_diff, form questions       │
│    Phase 2: INVESTIGATE — spawn reviewer(s)          │
│    Phase 3: JUDGE — consolidate, reply, done         │
│       │                                              │
│       ├── spawn_agent("reviewer", focus="...")        │
│       │      └── ReAct loop: tools + SGR             │
│       │                                              │
│       ├── [optional: spawn_many for parallel]        │
│       │                                              │
│       └── done(consolidated_findings)                │
│                                                      │
└──────────────────────────────────────────────────────┘
      │
      ▼
ReviewFinding[]      BLOCKER / MAJOR / MINOR / COMMENT
      │
      └──► post inline comments to PR
```

No pre-indexing. No database. One session per diff. All agents defined by `.prompt` files.

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

```bash
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
BITBUCKET_SERVER_BEARER_TOKEN=...
REQUESTS_CA_BUNDLE=/path/to/ca.pem        # optional
BITBUCKET_SERVER_CLIENT_CERT=/path/to/client.pem  # optional
```

### `config.local.yaml`

```yaml
llm:
  api_url: "https://api.deepseek.com/v1"
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"

review:
  max_steps: 40
  max_tokens: 40000
```

---

## CLI

```bash
python cli.py run --repo ./my-service --diff changes.diff
python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42
python cli.py run --pr-url ... --post-comments
python cli.py run --repo . --diff my.diff --output findings.json
python cli.py inspect changes.diff    # parse only, no LLM
```

| Flag | Description |
|------|-------------|
| `--repo` / `-r` | Path to repository |
| `--diff` / `-d` | Diff file path, or `-` for stdin |
| `--pr-url` | Bitbucket Server PR URL |
| `--post-comments` | Post findings as inline PR comments |
| `--model` / `-m` | LLM model override |
| `--api-url` | API base URL override |
| `--api-key` | API key override |
| `--output` / `-o` | Write findings as JSON |
| `--max-steps` | Max ReAct tool calls |
| `--max-tokens` | Max token budget |

---

## How it works

### Three-phase review methodology

**Phase 1 — ANALYZE:** The strategist reads the diff, identifies the system type, and formulates ALL investigation questions in a single reflect() call. This is the complete scope.

**Phase 2 — INVESTIGATE (one round):** Based on the analysis, the strategist spawns reviewer agent(s). All questions in one area → one reviewer. Different domains → spawn_many. No iterative spawning — one round of investigation.

**Phase 3 — JUDGE (no going back):** The strategist resolves questions from the evidence collected, handles PR comment threads, deduplicates findings, and delivers the verdict. New questions from results are answered from collected evidence, not by spawning more reviewers.

### Agents (defined by `.prompt` files)

**Strategist** — react agent with `spawn`, `observe_agents`, `adjust_agent` capabilities. Orchestrates the review. Owns PR comment interaction (`reply_to_comment`, `resolve_comment`).

**Reviewer** — focused react agent with SGR. Gets a specific focus from the strategist, investigates using repo tools (`find_files`, `read_file`, `read_outline`, `search`, `get_diff`), returns findings. No spawning, no PR interaction.

### SGR (Self-Guided Reasoning)

Every react agent tracks its reasoning via `reflect()`:

- `learned` — facts established so far
- `questions_remaining` — open questions
- `resolved_questions` — each with `resolution` (answered/dropped) and `summary`
- `confidence` — low / medium / high
- `next_action` — what to do next

Every open question must be explicitly resolved. No silent drops.

### CLI live display

Single live panel per agent (SGR top, actions bottom):

```
╭──────────────── reviewer · step 6/30 · 20% · ↑3246 · conf=high ────────────────╮
│ SGR · medium → high                                                              │
│   ✓ Order model items null? → @Builder.Default, never null                       │
│   ✓ Other getItems usages? → createOrder:29, PricingService:23                   │
│   ● releaseInventory behavior?                              step 3  new          │
│                                                                                  │
│   step 0  get_diff  ↑1823 ↓20                                                   │
│   step 1  read_outline(OrderService.java)  ↑2042 ↓50                            │
│   step 2  read_file(OrderService.java)  ↑2231 ↓49                               │
│   step 3  reflect()  conf=medium                                                 │
│   step 4  find_files(**/*.java)  ×1  ↑3165 ↓39                                  │
│   ↳ step 5  search("getItems")  ↓22…                                            │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

When an agent finishes, the panel (SGR + actions) is printed permanently to the log. The live frame switches to the next active agent.

### Incremental review

Existing PR comments are passed to the strategist. In Phase 3, the strategist:
- `resolve_comment(id)` — when the issue is addressed by the diff
- `reply_to_comment(id, text)` — when a fix is incomplete

---

## Orchestra Framework

DiffGraph is built on **Orchestra** (~3,700 LOC), a prompt-defined agent framework. Agents are defined entirely by `.prompt` files with `@` headers. No topologies, no pipelines — agents create structure at runtime via tool calls.

### Prompt file format

```
@agent: reviewer
@mode: react
@capabilities: sgr
@tools: find_files, read_file, search, get_diff
@budget: 30000 tokens, 30 steps
@llm: temperature=0
@data:
  diff_summary: string — changed files with line counts
  focus: string — specific task from strategist
@summary: Focused code reviewer. Investigates one aspect of a PR.
---
You are a code reviewer investigating a specific aspect.

{diff_summary}

YOUR TASK:
{focus}

...
```

`@data` fields serve triple duty: input schema for `spawn_agent`, `{placeholder}` injection into the prompt, and documentation for `list_agents`.

### LLM compiler

At startup, `.prompt` files are compiled into an agent registry. Two-pass parsing: deterministic regex for `@` headers + LLM fallback for unstructured prompts. Cached by file hash.

### Meta-tools

| Tool | What it does |
|---|---|
| `spawn_agent` | Create a child agent. Data fields injected into prompt `{placeholders}`. `"inherit"` copies from parent. |
| `spawn_many` | Fan-out N agents in parallel, return merged results |
| `plan` | Single-shot planner returning structured JSON tasks |
| `fork` | Clone self into N parallel branches with different focus |
| `adjust_agent` | Change a child's temperature, penalties, model, inject message, extend budget |
| `observe_agents` | Get status of all children: step, budget, SGR, signals |
| `list_agents` | Get the compiled agent registry (summaries, input schemas) |
| `reflect` | SGR self-reflection |
| `done` | Submit output and stop |

### Mutable LLM params

Every agent's generation parameters (temperature, penalties, model) are mutable at runtime. A supervisor agent can `adjust_agent(id, temperature=0.8, frequency_penalty=1.5)` to steer a stuck child. All changes logged as `param_adjusted` events.

### Behavioral signals (read-only)

| Signal | What it detects |
|---|---|
| `repetition_score` | Same tools/args called repeatedly |
| `progress_score` | Unique files explored, questions resolved |
| `stuck` | High repetition + low progress |

Available via `observe_agents`. Framework does not act on them — supervisor agents decide.

See [REQUIREMENTS.md](REQUIREMENTS.md) for the full technical specification.

---

## Supported languages

Java · Python · TypeScript / TSX · Go · Kotlin · Ruby · C#

(tree-sitter structural outlines; plain line-count fallback for unknown extensions)

---

## Architecture

```
orchestra/                   Prompt-defined agent framework (~3,700 LOC)
├── compiler.py              LLM compiler: .prompt files → agent registry
├── types.py                 Core dataclasses (AgentConfig, BudgetConfig, LLMParamsConfig)
├── config.py                YAML loading, env var expansion, validation
├── events.py                EventBus with typed events
├── agent.py                 Agent: single + react, all meta-tools built-in
├── budget.py                BudgetState, BudgetTracker, child budget partitioning
├── sgr.py                   SGR tracker with extensions + handoff extraction
├── handoff.py               7 context handoff modes + compose
├── condensation.py          4 message condensation strategies
├── streaming.py             LLM streaming with param passthrough
├── feedback.py              Read-only behavioral signals
├── merge.py                 Fork/join merge strategies
├── prompts.py               Template loading + {var} interpolation
└── tools/
    ├── registry.py          @register decorator, schema generation
    ├── builtin.py           Meta-tool schemas (spawn, adjust, observe, etc.)
    └── shared.py            AppendLog, MutexMap, Blackboard (swarm tools)

diffgraph/                   Code review domain
├── api.py                   DiffGraph public API
├── orchestrator.py          One agent entry point (~35 lines of logic)
├── orchestra_tools.py       Domain tools as closures
├── diff_parser.py           git diff → DiffResult
├── lang.py                  Language detection
├── tools.py                 Filesystem primitives
├── outline.py               tree-sitter structural outline
├── bitbucket.py             Bitbucket Server integration
└── prompts/
    ├── strategist.prompt    Three-phase review lead (analyze → investigate → judge)
    └── reviewer.prompt      Focused investigator with SGR
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```
