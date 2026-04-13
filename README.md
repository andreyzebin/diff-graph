# DiffGraph

Multi-agent PR code reviewer powered by the **Orchestra** framework. Takes a git diff (or a Bitbucket Server PR URL), spawns a lead agent that analyzes the change, delegates investigation to focused reviewer agents, and consolidates findings into a deduplicated list.

```
git diff / PR URL
      |
      v
parse_diff()         changed files + changed lines
      |
      v
+----------------------- Orchestra -----------------------+
|                                                          |
|  lead (react)                                      |
|    Phase 1: ANALYZE  -- read diff, form concerns         |
|    Phase 2: INVESTIGATE -- spawn reviewer(s), one round  |
|    Phase 3: JUDGE -- consolidate, reply, done            |
|       |                                                  |
|       +-- spawn_agent("reviewer", focus="concern")       |
|       |      +-- ReAct loop: tools + SGR                 |
|       |                                                  |
|       +-- [optional: spawn_many for parallel]            |
|       |                                                  |
|       +-- done(consolidated_findings)                    |
|                                                          |
+----------------------------------------------------------+
      |
      v
ReviewFinding[]      BLOCKER / MAJOR / MINOR / COMMENT
      |
      +---> post inline comments to PR
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
# edit .env -- fill in API keys
source .env

cp config.yaml config.local.yaml
# edit config.local.yaml -- set api_url and model if not using OpenAI
```

Run against a local repo:

```bash
python cli.py run --repo . --base HEAD~1
python cli.py run --repo . --base main --source feature/my-branch
```

Run against a Bitbucket Server PR:

```bash
source .env
python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42
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

### `run` -- review code changes

Two modes: PR (fetches everything from Bitbucket) or local repo (you specify refs).

```bash
# PR mode — clones repo, computes diff, fetches existing comments
python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42
python cli.py run --pr-url ... --post-comments

# Local mode — diff computed from git refs, repo not modified
python cli.py run --repo . --base HEAD~1
python cli.py run --repo . --base main --source feature/my-branch
python cli.py run --repo /path/to/service --base abc123 --source def456 --output findings.json
```

| Flag | Description |
|------|-------------|
| `--pr-url` | Bitbucket Server PR URL (PR mode) |
| `--repo` / `-r` | Path to local repository (local mode) |
| `--base` | Base ref — commit/branch to merge into. Required for local mode. |
| `--source` | Source ref — commit/branch being reviewed. Default: HEAD. |
| `--post-comments` | Post findings as inline PR comments (requires `--pr-url`) |
| `--model` / `-m` | LLM model override |
| `--api-url` | API base URL override |
| `--api-key` | API key override |
| `--output` / `-o` | Write findings as JSON |
| `--max-steps` | Max ReAct tool calls |
| `--max-tokens` | Max token budget |
| `--prompts` | Prompt resource URI (path, `file://`, `bitbucket://`) |

### `trace` -- view execution traces

```bash
python cli.py trace              # open last run in browser (starts trace server)
python cli.py trace --log        # print trace to console (call/result per step, agent tree)
python cli.py trace --list       # list recent runs
python cli.py trace --run ID     # specific run
```

### Prompt versioning

Load prompts from different sources for A/B testing:

```bash
python cli.py run --pr-url ... --prompts /path/to/prompts/v2
python cli.py run --pr-url ... --prompts file:///absolute/path/to/prompts
python cli.py run --pr-url ... --prompts bitbucket://server/PROJECT/prompts-repo/refs/main/prompts
```

### Comment traceability

Every posted comment includes a metadata tag at the end:

```
`dg:prompts:f7917d6:ae0bd23d-8d9`
```

Format: `` `dg:<generation>:<prompt_hash>:<run_id>` ``

- **generation** — prompt source name (last segment of `--prompts` URI or directory)
- **prompt_hash** — first 7 chars of content hash (md5) or commit SHA (Bitbucket provider)
- **run_id** — trace DB run ID

Extract from raw comment text (Bitbucket API):

```python
import re
m = re.search(r'`dg:(\S+):(\w+):([\w-]+)`', comment_text)
if m:
    gen, prompt_hash, run_id = m.group(1), m.group(2), m.group(3)
```

Enables pr-analytics to correlate comment acceptance rates with prompt generations. Same `gen:hash` across comments = same prompt version. Different hash = prompt was modified (mutation).

---

## How it works

### Three-phase review methodology

**Phase 1 -- ANALYZE:** The lead reads the diff, identifies the system type, and formulates concerns scaled to diff size: 1-2 for small diffs, 2-3 for medium, 3-5 for large. Each concern is a distinct theme — not split facets of the same issue.

**Phase 2 -- INVESTIGATE (one round):** The lead spawns reviewer agent(s), each getting one concern as its focus. The reviewer breaks the concern into sub-questions and investigates using repo tools. One round of investigation -- no iterative spawning.

**Phase 3 -- JUDGE (no going back):** The lead resolves concerns from the evidence collected, handles PR comment threads, deduplicates findings, and delivers the verdict. New questions from results are answered from collected evidence, not by spawning more reviewers.

### Agents (defined by `.prompt` files)

**Lead** -- react agent with `spawn`, `observe_agents`, `adjust_agent` capabilities. Orchestrates the review. Owns PR comment interaction (`reply_to_comment`, `resolve_comment`).

**Reviewer** -- focused react agent with SGR. Gets a concern as focus, investigates first (get_diff, read_outline), then reflects with only genuinely unknown questions. Resolved questions from previous reflects carry concrete answers. No spawning, no PR interaction, no lead SGR context (clean start).

### SGR (Self-Guided Reasoning)

Every react agent tracks its reasoning via `reflect()`:

- `learned` -- facts established so far
- `questions_remaining` -- open questions (each with a stable question ID)
- `resolved_questions` -- each with `resolution` (answered/dropped) and `summary`
- `confidence` -- low / medium / high
- `next_action` -- what to do next

Question IDs provide stability across reflect calls -- fuzzy matching links questions across steps even when wording drifts. Every open question must be explicitly resolved. No silent drops.

### Budget

Agents use their own `.prompt` budget (not parent-allocated). Default pushers: 75% nudge + 100% force_done. Budget tracks cumulative paid (sum of per-step deltas) with cache discount.

### CLI live display

Child events suppressed in CLI -- only root agent visible. Single live panel per agent (SGR top, actions bottom):

```
+----------------- reviewer . step 6/30 . 20% . ^3246 . conf=high -----------------+
| SGR . medium -> high                                                              |
|   OK  Order model items null? -> @Builder.Default, never null                     |
|   OK  Other getItems usages? -> createOrder:29, PricingService:23                 |
|   *   releaseInventory behavior?                              step 3  new         |
|                                                                                   |
|   step 0  get_diff  ^1823 v20                                                     |
|   step 1  read_outline(OrderService.java)  ^2042 v50                              |
|   step 2  read_file(OrderService.java)  ^2231 v49                                 |
|   step 3  reflect()  conf=medium                                                  |
|   step 4  find_files(**/*.java)  x1  ^3165 v39                                    |
|   > step 5  search("getItems")  v22...                                            |
+-----------------------------------------------------------------------------------+
```

### Incremental review

Existing PR comments are passed to the lead. In Phase 3, the lead:
- `resolve_comment(id)` -- when the issue is addressed by the diff
- `reply_to_comment(id, text)` -- when a fix is incomplete

---

## Orchestra Framework

DiffGraph is built on **Orchestra** (~3,700 LOC), a prompt-defined agent framework. Agents are defined entirely by `.prompt` files with `@` headers. No topologies, no pipelines -- agents create structure at runtime via tool calls.

### Prompt file format

```
@agent: reviewer
@mode: react
@capabilities: sgr
@tools: find_files, read_file, search, get_diff
@budget: 30000 tokens, 30 steps
@llm: temperature=0
@data:
  diff_summary: string -- changed files with line counts
  focus: string -- specific task from lead
@summary: Focused code reviewer. Investigates one aspect of a PR.
---
You are a code reviewer investigating a specific aspect.

{diff_summary}

YOUR TASK:
{focus}

...
```

`@data` fields serve triple duty: input schema for `spawn_agent`, `{placeholder}` injection into the prompt, and documentation for `list_agents`. Data inheritance: parent's data_scope auto-injected into child `{placeholders}`.

### LLM compiler

At startup, `.prompt` files are compiled into an agent registry. Two-pass parsing: deterministic regex for `@` headers + LLM fallback for unstructured prompts. Cached by file hash.

### Meta-tools (9 total)

| Tool | What it does |
|---|---|
| `spawn_agent` | Create a child agent. Data fields injected into prompt `{placeholders}`. `"inherit"` copies from parent. |
| `spawn_many` | Fan-out N agents in parallel, return merged results |
| `plan` | Single-shot planner returning structured JSON tasks |
| `fork` | Clone self into N parallel branches with different focus |
| `adjust_agent` | Change a child's temperature, penalties, model, inject message, extend budget |
| `observe_agents` | Get status of all children: step, budget, SGR, signals |
| `list_agents` | Get the compiled agent registry (summaries, input schemas) |
| `reflect` | SGR self-reflection with question IDs |
| `done` | Submit output and stop |

### Mutable LLM params

Every agent's generation parameters (temperature, penalties, model) are mutable at runtime. A supervisor agent can `adjust_agent(id, temperature=0.8, frequency_penalty=1.5)` to steer a stuck child. All changes logged as `param_adjusted` events.

### Trace system

SQLite trace DB persists events per-step (crash-safe). Two web views:

- **Navigator** (`/runs/{id}/trace`) -- split-pane: agent tree left, detail tabs right. Click `[⧉]` to load full data from API. Right panel has `📋 Copy` and `{ } JSON` toggle. Steps show tool args preview, token usage (`↑` new input, `↓` output, `©` cached).
- **Live** (`/runs/{id}/live`) -- real-time event stream via WebSocket. Bulk-loads existing events on open, then streams new ones. Child agents color-coded with `[reviewer:Focus]` tags. Auto-scroll pauses when scrolling up.

Both views link to each other. Runs list (`/`) auto-refreshes every 3s. Console trace via `--log`.

### Behavioral signals (read-only)

| Signal | What it detects |
|---|---|
| `repetition_score` | Same tools/args called repeatedly |
| `progress_score` | Unique files explored, questions resolved |
| `stuck` | High repetition + low progress |

Available via `observe_agents`. Framework does not act on them -- supervisor agents decide.

See [REQUIREMENTS.md](REQUIREMENTS.md) for the full technical specification.

---

## Supported languages

Java, Python, TypeScript / TSX, Go, Kotlin, Ruby, C#

(tree-sitter structural outlines; plain line-count fallback for unknown extensions)

---

## Architecture

```
orchestra/                   Prompt-defined agent framework (~3,700 LOC)
+-- compiler.py              LLM compiler: .prompt files -> agent registry
+-- trace.py                 Trace data collection + template preparation
+-- trace_db.py              SQLite trace storage + reader
tracing/                     Trace CLI + web server
+-- server/                  FastAPI trace viewer (Alpine.js + Jinja2)
    +-- app.py               Routes, data API, WebSocket live updates
    +-- templates/            Jinja2 templates (trace, macros, runs, live)
    +-- static/               CSS + JS
+-- types.py                 AgentConfig, BudgetConfig, LLMParamsConfig
+-- config.py                YAML loading, env var expansion, validation
+-- events.py                EventBus with typed events
+-- agent.py                 Agent: single + react, all meta-tools built-in
+-- budget.py                BudgetState with cumulative_paid
+-- sgr.py                   SGR with question IDs + fuzzy matching
+-- handoff.py               7 context handoff modes
+-- condensation.py          4 message condensation strategies
+-- streaming.py             LLM streaming with param passthrough
+-- feedback.py              Read-only behavioral signals
+-- merge.py                 Merge strategies (union, best_confidence, llm_merge, raw)
+-- prompts.py               Template loading + regex interpolation
+-- tools/
    +-- registry.py          @register decorator, schema generation
    +-- builtin.py           Meta-tool schemas (spawn, adjust, observe, etc.)
    +-- shared.py            AppendLog, MutexMap, Blackboard

diffgraph/                   Code review domain
+-- api.py                   DiffGraph public API
+-- orchestrator.py          One agent entry point (~35 lines of logic)
+-- orchestra_tools.py       Domain tools as closures
+-- diff_parser.py           git diff -> DiffResult
+-- lang.py                  Language detection
+-- tools.py                 Filesystem primitives
+-- outline.py               tree-sitter structural outline
+-- bitbucket.py             Bitbucket Server integration
+-- prompts/
    +-- lead.prompt    Three-phase review lead (analyze -> investigate -> judge)
    +-- reviewer.prompt      Focused investigator with SGR
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```
