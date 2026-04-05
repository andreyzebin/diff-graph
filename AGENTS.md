# Agent Guide

This document describes the codebase for AI agents and coding assistants.

## What this project does

DiffGraph is a multi-agent PR code reviewer. It takes a raw `git diff` (or fetches one
from Bitbucket Server), runs a two-phase pipeline with two specialized agents, and produces
structured `ReviewFinding` objects — optionally posted as inline PR comments.

**Agents:**
- **Strategist** — one non-streaming LLM call; reads the diff summary and outputs a typed
  review plan (system type + task list).
- **Solver** — ReAct loop; uses 9 tools to explore the repo, calls `reflect()` for
  self-guided reasoning, and submits findings via `done()`.

No pre-indexing, no database, no persistent state. One `DiffGraph.review()` call per diff.

## Repository layout

```
diffgraph/
├── api.py               # DiffGraph public API class
├── diff_parser.py       # parse_diff() — git diff text → DiffResult
├── lang.py              # language detection + file extension map
├── tools.py             # list_files, read_file, search_text — filesystem primitives
├── outline.py           # get_outline() — tree-sitter structural outline
├── streaming.py         # stream_llm() — shared streaming helper
├── orchestrator.py      # run_review(): plan phase + ReAct solve phase
├── bitbucket.py         # fetch_pr, post/get/reply/resolve PR comments
└── prompts/
    ├── __init__.py      # load(name) helper
    ├── strategist_system.txt   # plan phase prompt
    └── orchestrator_system.txt # solve phase ReAct + SGR prompt
tests/
└── test_diff_parser.py
cli.py              # Typer CLI: run / inspect
config.yaml         # Committed defaults with ${VAR} placeholders
config.local.yaml   # Local overrides — gitignored
.env.example        # Environment variable template
```

## Data flow

```
parse_diff(diff_text)
  └─► DiffResult
        ├─ changed_files         → file paths with status
        ├─ files[path].status    → "added"/"modified"/"deleted"/"renamed"
        └─ files[path].after_changed_lines  → set of + line numbers

run_review(diff_text, repo_path, llm, model, existing_comments?)
  ├─► _plan_phase()
  │     └─► single LLM call (no tools) → JSON plan
  │           ├─ system_type: "spring-service" | "react-app" | ...
  │           └─ tasks: [{id, type, priority, focus, search_hints}]
  │
  └─► _solve_phase(plan)
        └─► ReAct loop (up to max_steps)
              ├─ find_files / read_file / read_outline / search / get_diff
              ├─ reply_to_comment / resolve_comment
              ├─ reflect()   ← SGR self-reflection, no side effects
              └─ done(findings)  ← exits loop, returns ReviewFinding list
```

## Key abstractions

### `DiffResult` (`diff_parser.py`)

- `changed_files` — after-paths (excludes deleted)
- `files[path].status` — `"added"` / `"modified"` / `"deleted"` / `"renamed"`
- `files[path].after_changed_lines` — set of 1-indexed `+` line numbers
- `files[path].hunks` — list of `HunkSnippet` with `before_lines` / `after_lines`

### `ReviewFinding` (`orchestrator.py`)

Output of the solve phase. Fields:
- `file` — relative path
- `line` — most relevant line number in the changed code
- `severity` — `"BLOCKER"` / `"MAJOR"` / `"MINOR"` / `"COMMENT"`
- `title` — one-line summary
- `explanation` — what the problem is and why it matters
- `evidence` — code evidence supporting the finding
- `suggestion` — optional concrete fix

### `ReviewContext` (`orchestrator.py`)

Collected side-effectful actions from the solve phase:
- `comment_replies` — `[{comment_id, text}]` to POST after the run
- `comment_resolves` — `[comment_id]` to mark resolved after the run

### `stream_llm` (`streaming.py`)

Shared helper for all LLM calls with tool use. Wraps `llm.chat.completions.create(stream=True)`
and assembles a `StreamedResponse` compatible with the non-streaming OpenAI interface.
Fires `on_token(tool_name, args_so_far, chunk_count)` per chunk for live display.
Usage is extracted from the final chunk via `stream_options={"include_usage": True}`.

### `get_outline` (`outline.py`)

Tree-sitter structural outline of a source file. Returns plain text:
```
# src/Foo.java  (120 lines)
[class] FooService  L10-118
  [method] process  L15-40 *
  [method] validate  L42-60
```
`*` marks symbols overlapping `changed_lines`. Session-cached (keyed by `repo_path/path`)
for files without changed lines. Falls back to a line-count header when tree-sitter
is unavailable or the language is unsupported.

### Plan phase (`orchestrator._plan_phase`)

Single non-streaming LLM call using `strategist_system.txt`. Input: compact diff summary
(file list + `+N -N` totals + first 200 diff lines). Output: JSON plan with `system_type`
and a list of typed tasks. Falls back to a default `business_logic` task on parse failure.

### Solve phase (`orchestrator._solve_phase`)

ReAct loop. Each iteration:
1. `stream_llm(tools=_SOLVE_TOOLS, tool_choice="required")`
2. Separate `done` from dispatchable tool calls
3. Emit events for all tool calls (`orchestrator_step` / `orchestrator_reflect`)
4. Execute dispatchable calls in parallel via `ThreadPoolExecutor`
5. Append assistant + tool result messages
6. If `done` was called → parse findings and return

Adaptive budget: user-message nudges at 50% and 75% of `max_tokens`.
Force-done at `max_steps`: re-calls with only `done` in the tool list.

### SGR — Self-Guided Reasoning

The `reflect` tool lets the agent structure its own reasoning mid-loop:
```json
{
  "learned": "StoreCreditService.apply() is called from OrderController",
  "questions_remaining": ["Is the balance check atomic?"],
  "confidence": "medium",
  "next_action": "Read the transaction boundary around apply()"
}
```
Always returns `"Reflection noted."` — no side effects. The value is in forcing
the agent to articulate its state before the next tool call.

### `bitbucket.py`

`fetch_pr(pr_url)`:
1. REST API for PR metadata (title, description, fromRef/toRef SHAs)
2. `git clone --filter=blob:none --single-branch` of the source branch
3. Auth baked into repo config for lazy blob fetches
4. `git fetch --filter=blob:none origin <toRef_sha>` for merge-base
5. `git diff toRef...fromRef` (three-dot = merge-base diff matching PR UI)

`post_review_comments(pr_url, comments, changed_lines?)`:
- `changed_lines` maps `file → set[int]` from `diff_result`. Each comment's line is
  snapped to the nearest changed line so the Bitbucket anchor is valid.
- Falls back to a general (un-anchored) PR comment if the file has no changed lines.
- Internal severity is mapped to Bitbucket's two values: `BLOCKER`/`MAJOR` → `BLOCKER`,
  `MINOR`/`COMMENT` → `NORMAL`.

`get_pr_comments(pr_url)` — fetches existing comment threads via the activities API.

`reply_to_pr_comment(pr_url, comment_id, text)` — POST to `/comments` with `parent: {id}`.

`resolve_pr_comment(pr_url, comment_id)` — PUT with `state: RESOLVED` (optimistic lock).

## Event system

All events are fired via `on_event(event, **kwargs)`. Unknown events are silently ignored.

| Event | Key kwargs |
|-------|-----------|
| `orchestrator_plan_start` | — |
| `orchestrator_plan_done` | `plan` |
| `orchestrator_stream` | `step`, `tool_name`, `args_preview`, `tok` |
| `orchestrator_step` | `step`, `tool`, `args`, `tok_in`, `tok_out`, `tok_cached` |
| `orchestrator_reflect` | `step`, `learned`, `questions_remaining`, `confidence`, `next_action` |
| `orchestrator_result` | `step`, `tool`, `result_len` |
| `orchestrator_done` | `findings`, `replies`, `resolves` |
| `orchestrator_forced_done` | `reason`, `tok_in`, `tok_out`, `tok_cached` |

## Common tasks

**Add a language:**
1. `lang.py`: add to `LANG_MAP` and `FILE_EXTENSIONS`
2. `outline.py`: add to `_TS_LANG`, `_CONTAINERS`, `_MEMBERS`

**Change what the agent looks for:**
Edit `prompts/strategist_system.txt` — task types, system type examples, rules.

**Change agent behaviour / tool descriptions:**
Edit `prompts/orchestrator_system.txt` — workflow, severity guide, rules.
Prompts are loaded once at import time via `prompts/__init__.py:load()`.

**Add a new tool:**
1. Add entry to `_SOLVE_TOOLS` list in `orchestrator.py`
2. Handle it in `_dispatch()`
3. Document it in `prompts/orchestrator_system.txt`

**Add a new event:**
Emit `on_event("my_event", **kwargs)` anywhere. Handle it in `cli.py:_make_event_handler`.
No schema — callers ignore unknown events.

**Add a new provider:**
Create `diffgraph/<provider>.py` following the pattern in `bitbucket.py`:
`fetch_pr()` returns `(diff_text, repo_path, cleanup_fn, pr_meta)`.

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Covers `diff_parser` without an LLM. To test the full pipeline, point at a real
LLM endpoint and run `cli.py run` against a local diff.
