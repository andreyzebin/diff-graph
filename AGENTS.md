# Agent Guide

This document describes the codebase for AI agents and coding assistants.

## What this project does

DiffGraph is a multi-agent PR code reviewer built on the Orchestra framework. It takes a raw `git diff` (or fetches one from Bitbucket Server), runs a three-phase review pipeline via prompt-defined agents, and produces structured `ReviewFinding` objects -- optionally posted as inline PR comments.

**Agents (defined by `.prompt` files):**
- **Lead** (react) -- three-phase review lead: analyze the diff, form concerns scaled to diff size (1-2 small, 2-3 medium, 3-5 large), spawn reviewer(s) without SGR handoff (one round), consolidate and judge.
- **Reviewer** (react with SGR) -- focused investigator. Gets one concern, investigates first (get_diff, read_outline), then reflects with only genuinely unknown questions. Returns findings with evidence. No spawning, no PR interaction.

No pre-indexing, no database, no persistent state. One `run_review()` call per diff. The orchestrator is ~35 lines of logic -- all methodology lives in the `.prompt` files.

## Repository layout

```
orchestra/                       Prompt-defined agent framework (~3,700 LOC)
+-- compiler.py                  LLM compiler: .prompt files -> agent registry
+-- trace.py                     Trace data collection + template preparation
+-- trace_db.py                  SQLite trace storage + reader
+-- trace_server/                FastAPI trace viewer (Alpine.js + Jinja2)
    +-- app.py                   Routes, data API, WebSocket live updates
    +-- templates/               Jinja2: trace.html, macros.html, runs.html, live.html
    +-- static/                  trace.css
+-- types.py                     AgentConfig, BudgetConfig, LLMParamsConfig
+-- config.py                    YAML loading, env var expansion, validation
+-- events.py                    EventBus with typed events
+-- agent.py                     Agent: single + react, all meta-tools built-in
+-- budget.py                    BudgetState with cumulative_paid
+-- sgr.py                       SGR with question IDs + fuzzy matching
+-- handoff.py                   7 context handoff modes
+-- condensation.py              4 message condensation strategies
+-- streaming.py                 LLM streaming with param passthrough
+-- feedback.py                  Read-only behavioral signals
+-- merge.py                     Merge strategies (union, best_confidence, llm_merge, raw)
+-- prompts.py                   Template loading + regex interpolation
+-- tools/
    +-- registry.py              @register decorator, schema generation
    +-- builtin.py               Meta-tool schemas (spawn, adjust, observe, etc.)
    +-- shared.py                AppendLog, MutexMap, Blackboard

diffgraph/                       Code review domain
+-- api.py                       DiffGraph public API class
+-- orchestrator.py              One agent entry point (~35 lines of logic)
+-- orchestra_tools.py           Domain tools as closures
+-- diff_parser.py               git diff -> DiffResult
+-- lang.py                      Language detection + file extension map
+-- tools.py                     Filesystem primitives (list_files, read_file, search_text)
+-- outline.py                   tree-sitter structural outline
+-- bitbucket.py                 Bitbucket Server integration
+-- prompts/
    +-- lead.prompt        Three-phase review lead (analyze -> investigate -> judge)
    +-- reviewer.prompt          Focused investigator with SGR

tests/
+-- test_diff_parser.py

cli.py                           Typer CLI: run / trace / inspect
config.yaml                      Committed defaults with ${VAR} placeholders
config.local.yaml                Local overrides (gitignored)
.env.example                     Environment variable template
```

## Data flow

```
parse_diff(diff_text)
  +-> DiffResult
        +- changed_files         -> file paths with status
        +- files[path].status    -> "added"/"modified"/"deleted"/"renamed"
        +- files[path].after_changed_lines  -> set of + line numbers

run_review(diff_text, repo_path, llm, model, existing_comments?)
  +-> compile_prompts()          -> agent registry from .prompt files
  +-> register_diffgraph_tools() -> domain tools as closures over context
  +-> build lead config    -> inject diff_summary, existing_comments
  +-> Agent(lead).run()
        |
        Lead (react):
          Phase 1: ANALYZE -- read diff, form 3-5 concerns
          Phase 2: INVESTIGATE -- spawn_agent("reviewer", focus=concern)
              +-> Reviewer (react + SGR):
                    ReAct loop: find_files, read_file, read_outline,
                    search, get_diff, reflect(), done(findings)
          Phase 3: JUDGE -- consolidate, deduplicate, done(findings)
        |
  +-> parse findings -> list[ReviewFinding]
  +-> ReviewContext (comment_replies, comment_resolves)
```

Data inheritance: parent's `data_scope` is auto-injected into child `{placeholders}`. No handoff context by default -- child gets everything via its system prompt.

## Key abstractions

### `DiffResult` (`diff_parser.py`)

- `changed_files` -- after-paths (excludes deleted)
- `files[path].status` -- `"added"` / `"modified"` / `"deleted"` / `"renamed"`
- `files[path].after_changed_lines` -- set of 1-indexed `+` line numbers
- `files[path].hunks` -- list of `HunkSnippet` with `before_lines` / `after_lines`

### `ReviewFinding` (`orchestrator.py`)

Output of the review. Fields:
- `file` -- relative path
- `line` -- most relevant line number in the changed code
- `severity` -- `"BLOCKER"` / `"MAJOR"` / `"MINOR"` / `"COMMENT"`
- `title` -- one-line summary
- `explanation` -- what the problem is and why it matters
- `evidence` -- code evidence supporting the finding
- `suggestion` -- optional concrete fix

### `ReviewContext` (`orchestrator.py`)

Side-effectful actions collected during the review:
- `comment_replies` -- `[{comment_id, text}]` to POST after the run
- `comment_resolves` -- `[comment_id]` to mark resolved after the run

### Agent (`orchestra/agent.py`)

Two modes:
- **single** -- one LLM call, no tools
- **react** -- non-deterministic tool loop with SGR

All 9 meta-tools built in: `spawn_agent`, `spawn_many`, `plan`, `fork`, `adjust_agent`, `observe_agents`, `list_agents`, `reflect`, `done`.

### SGR with question IDs (`orchestra/sgr.py`)

Structured self-reflection. Each question gets a stable ID. Fuzzy matching links questions across reflect() calls even when wording drifts. Fields: `learned`, `questions_remaining`, `resolved_questions`, `confidence`, `next_action`.

### Budget (`orchestra/budget.py`)

Tracks cumulative paid (sum of per-step deltas) with cache discount. Agents use their own `.prompt` budget. Default pushers: 75% nudge + 100% force_done.

### Trace system (`orchestra/trace_db.py`, `orchestra/trace.py`, `orchestra/trace_server/`)

SQLite DB persists events per-step (crash-safe). FastAPI trace server with Alpine.js frontend. Two views:

**Navigator** (`/runs/{id}/trace`):
- Split-pane: agent tree left, detail tabs right (draggable divider)
- `[⧉]` buttons load full data on demand from API (messages, tool calls, results)
- Right panel toolbar: `📋 Copy` to clipboard, `{ } JSON` toggle
- Tool call args pretty-printed; message content shown as plain text
- Token usage per step: `↑` new input, `↓` output, `©` cached
- Agent header shows totals: `↑total_in ↓total_out ©total_cached`

**Live** (`/runs/{id}/live`):
- Real-time event stream via WebSocket
- Bulk-loads existing events on open, then streams new ones
- Child agents color-coded with `[reviewer:Focus]` tags
- Tool args preview in event lines
- Auto-scroll pauses when user scrolls up

Runs list (`/`) auto-refreshes every 3s. Both views link to each other.

### `get_outline` (`outline.py`)

Tree-sitter structural outline of a source file. Returns plain text:
```
# src/Foo.java  (120 lines)
[class] FooService  L10-118
  [method] process  L15-40 *
  [method] validate  L42-60
```
`*` marks symbols overlapping `changed_lines`. Session-cached for unchanged files. Falls back to a line-count header when tree-sitter is unavailable.

### `bitbucket.py`

`fetch_pr(pr_url)`:
1. REST API for PR metadata (title, description, fromRef/toRef SHAs)
2. `git clone --filter=blob:none --single-branch` of the source branch
3. Auth baked into repo config for lazy blob fetches
4. `git fetch --filter=blob:none origin <toRef_sha>` for merge-base
5. `git diff toRef...fromRef` (three-dot = merge-base diff matching PR UI)

`post_review_comments(pr_url, comments, changed_lines?)`:
- Snaps each comment's line to nearest changed line for valid Bitbucket anchor
- Falls back to general PR comment if file has no changed lines
- Severity mapping: `BLOCKER`/`MAJOR` -> `BLOCKER`, `MINOR`/`COMMENT` -> `NORMAL`

`get_pr_comments`, `reply_to_pr_comment`, `resolve_pr_comment` -- thread interaction.

## Common tasks

### Add a language

1. `lang.py`: add to `LANG_MAP` and `FILE_EXTENSIONS`
2. `outline.py`: add to `_TS_LANG`, `_CONTAINERS`, `_MEMBERS`

### Add a new agent

1. Create `diffgraph/prompts/<name>.prompt` with `@` headers (see README for format)
2. The LLM compiler auto-discovers it -- no code changes needed
3. Other agents can find it via `list_agents` and spawn it via `spawn_agent`

### Add a new domain tool

1. Add the tool function in `diffgraph/orchestra_tools.py` using `@registry.register`
2. Reference the tool name in the agent's `@tools` header in its `.prompt` file
3. The tool is a closure over the review context (`_Ctx`)

### Change review methodology

Edit the `.prompt` files in `diffgraph/prompts/`:
- `lead.prompt` -- three-phase methodology, concern types, system type examples
- `reviewer.prompt` -- investigation workflow, severity guide, tool usage rules

All methodology lives in prompts, not in Python code. The orchestrator is ~35 lines.

### Change agent behavior at runtime

Parent agents can modify children via `adjust_agent`:
- Change temperature, penalties, model
- Inject a message into the child's conversation
- Extend the child's step budget

### Add a new provider

Create `diffgraph/<provider>.py` following the pattern in `bitbucket.py`:
`fetch_pr()` returns `(diff_text, repo_path, cleanup_fn, pr_meta)`.

### Inspect a run

```bash
python cli.py trace --log        # console trace: call/result per step, agent tree
python cli.py trace              # open last run in browser (starts trace server)
python cli.py trace --list       # recent runs table
python cli.py trace --run ID     # specific run
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Covers `diff_parser` without an LLM. To test the full pipeline, point at a real LLM endpoint and run `cli.py run` against a local diff.
