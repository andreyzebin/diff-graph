# Agent Guide

This document describes the codebase for AI agents and coding assistants.

## What this project does

DiffGraph builds a lightweight in-memory metamodel of the code touched by a PR.
It takes a raw `git diff` (or fetches one from Bitbucket Server), extracts entities
from the changed files using an LLM, recursively resolves dependencies via BFS,
runs a ReAct impact-analysis agent to find impacted callers, then either renders
a structured text context or runs a review curation agent to produce inline comments.

No pre-indexing, no database, no persistent state. One `DiffGraph.build()` call per review.

## Repository layout

```
diffgraph/
├── model.py             # Symbol, Module, MetaModel dataclasses
├── lang.py              # language detection + declaration patterns per language
├── tools.py             # list_files, read_file, search_text — filesystem primitives
├── diff_parser.py       # parse_diff() — git diff text → DiffResult
├── cache.py             # content-addressed extraction cache (~/.cache/diffgraph/)
├── explorer.py          # explore() BFS + explore_callers() impact agent orchestration
├── renderer.py          # render() — MetaModel → text prompt context
├── diffgraph.py         # DiffGraph public API + mark_changed_symbols()
├── agents/
│   ├── extractor.py     # extract_module() — LLM extraction with streaming + retry
│   ├── impact.py        # find_impact() — ReAct agent for caller/impact analysis
│   ├── review.py        # find_review_context() — ReAct agent for context curation
│   ├── planner.py       # plan_review() — single LLM call: strategy hint
│   ├── resolver.py      # resolve_dep() — agentic dependency path resolution
│   ├── reviewer.py      # generate_review_comments() — inline PR comment generation
│   ├── streaming.py     # stream_llm() — shared streaming helper, assembles StreamedResponse
│   └── prompts/         # all prompt text as .txt files with SECTION: blocks
│       ├── extract_system.txt
│       ├── impact_agent_system.txt
│       ├── review_agent_system.txt
│       ├── planner_system.txt
│       ├── reviewer_system.txt
│       ├── resolver_system.txt
│       └── render_context.txt
├── providers/
│   └── bitbucket_server.py  # fetch_pr() + post_review_comments()
tests/
├── test_diff_parser.py
├── test_mark_changed.py
└── test_renderer.py
cli.py              # Typer CLI: run / inspect
config.yaml         # Committed defaults with ${VAR} placeholders
config.local.yaml   # Local overrides — gitignored
```

## Data flow

```
parse_diff(diff_text)
  └─► DiffResult
        ├─ changed_files         → BFS start nodes
        ├─ files[path].status    → "added"/"modified"/"deleted"/"renamed"
        └─ files[path].hunks     → before_lines / after_changed_lines

explore(start_files, repo_path, llm, max_depth)
  └─► MetaModel (depth 0..max_depth)
        └─ modules: {path → Module}
              └─ symbols: [Symbol(start_line, end_line, kind, signature, summary)]

mark_changed_symbols(model, diff_result, repo_path)
  └─► symbol.is_changed = True for symbols overlapping after_changed_lines
      symbol.full_code  = read_file(after-version)

explore_callers(model, repo_path, llm, …, diff_result)
  └─► find_impact(modules, file_statuses, …)  ← single ReAct run for all changed files
        └─► ImpactHit list → extract caller modules → add to MetaModel at depth=-1

plan_review(changed_block, llm) → strategy string   ← single cheap LLM call

find_review_context(meta, repo_path, llm, strategy)
  └─► ReAct loop → list[ReviewSelection]

apply_selections(meta, selections) → marks symbol.is_expanded = True

render(model, diff_result, repo_path)
  └─► text context string (token-budget aware, partial compression)
```

## Key abstractions

### `DiffResult` (`diff_parser.py`)

- `changed_files` — after-paths for BFS (excludes deleted)
- `files[path].status` — `"added"` / `"modified"` / `"deleted"` / `"renamed"`
- `files[path].after_changed_lines` — set of `+` line numbers
- `files[path].hunks` — list of `HunkSnippet` with `before_lines` / `after_lines`

### `MetaModel` (`model.py`)

Flat dict `modules: {path → Module}`. Always the after-version.
`compute_depths()` returns `{path → depth}`: 0 = changed, 1..N = dependencies, -1 = callers.

### `stream_llm` (`agents/streaming.py`)

Shared helper for all tool-calling agents. Wraps `llm.chat.completions.create(stream=True)`
and assembles a `StreamedResponse` compatible with the non-streaming OpenAI interface.
Fires `on_token(tool_name, args_so_far, chunk_count)` per chunk for live display.
Usage is extracted from the final chunk via `stream_options={"include_usage": True}`.

### `extract_module` (`agents/extractor.py`)

One LLM call per file. Checks the content-addressed cache (`cache.py`) first — key is
SHA256(content + model). On miss: streams the LLM response, fires `on_event("token", …)`,
retries up to 2 times on invalid JSON. Saves to cache on success.

### `find_impact` (`agents/impact.py`)

ReAct loop for all changed modules at once. Tools: `list_files`, `search`, `read_file`, `done`.

Key behaviours:
- File statuses (`[ADDED]`/`[MODIFIED]`) are shown in the system prompt so the agent
  knows not to search for imports of brand-new classes.
- `read_file` on any excluded (changed) file returns a message instead of re-reading —
  the content is already in the system prompt.
- Parallel tool calls: all tool_calls from one LLM response run concurrently via
  `ThreadPoolExecutor`.
- Adaptive budget: user-message nudges at 50% and 75% of `max_tokens`.

### `find_review_context` (`agents/review.py`)

ReAct loop for context curation. Tools: `get_symbols`, `search`, `read_file`, `select`, `done`.

- `select(file, symbol_name, detail="full"|"summary")` is processed immediately
  (marks `symbol.is_expanded = True` on the MetaModel) and logged permanently.
- Parallel tool calls for `get_symbols` / `search` / `read_file`.
- Adaptive budget nudges at 50% / 75% of `max_agent_tokens`.
- Strategy hint from `plan_review()` is injected into the first user message.

### `render` (`renderer.py`)

Prompt text is loaded from `agents/prompts/render_context.txt` via `SECTION:name` blocks.
Partial compression: top-level symbols are compressed (body → `[omitted]`) unless
`is_expanded` or `is_changed`; nested expanded symbols are preserved within a
compressed outer class.

Token-budget degradation: full → depth-2 names only → depth-1 summaries only.
Changed files and callers are never cut.

### `resolve_dep` (`agents/resolver.py`)

Two-pass: fast glob lookup first, then agentic LLM search if ambiguous.
`is_likely_external()` pre-filters stdlib/third-party names before any LLM call.

### Bitbucket Server (`providers/bitbucket_server.py`)

`fetch_pr(pr_url)`:
1. REST API call for PR metadata (title, description, fromRef, toRef SHAs)
2. `git clone --filter=blob:none --single-branch` of the source branch
3. Auth baked into repo config (`git config http.extraHeader`) for lazy blob fetches
4. `git fetch --filter=blob:none origin <toRef_sha>` (full history for merge-base)
5. `git diff toRef...fromRef` (three-dot = merge-base diff matching PR UI)

`post_review_comments(pr_url, comments)`: REST POST with anchor
`{diffType: "EFFECTIVE", lineType: "ADDED", line: N}`.

## Event system

All agents fire `on_event(event: str, **kwargs)`. Unknown events are silently ignored.

| Event | Emitter | Key kwargs |
|-------|---------|-----------|
| `reading` | explorer | `path`, `depth` |
| `extracting` | extractor | `path`, `attempt` |
| `token` | extractor | `text` (accumulated) |
| `cache_hit` | extractor | `path`, `symbols` |
| `extracted` | extractor | `path`, `symbols`, `deps` |
| `retry` | extractor | `path`, `attempt`, `reason` |
| `failed` | extractor | `path` |
| `read_failed` | explorer | `path` |
| `skipped` | explorer | `path` |
| `resolving_agent` | resolver | `name`, `fqn` |
| `resolved` | resolver | `name`, `path`, `tok_in`, `tok_out` |
| `not_resolved` | resolver | `name` |
| `searching_callers` | explorer | `name`, `path` |
| `agent_stream` | impact | `step`, `path`, `tool_name`, `args_preview`, `tok` |
| `agent_step` | impact | `step`, `path`, `tool`, `args`, `tok_in`, `tok_out` |
| `agent_result` | impact | `step`, `tool`, `result_len` |
| `agent_done` | impact | `path`, `hits`, `tok_in`, `tok_out`, `tok_cached` |
| `agent_forced_done` | impact | `path`, `reason`, `tok_in`, `tok_out` |
| `caller_found` | explorer | `path`, `reason`, `confidence` |
| `review_start` | review | `changed` |
| `review_stream` | review | `step`, `tool_name`, `args_preview`, `tok` |
| `review_step` | review | `step`, `tool`, `args`, `tok_in`, `tok_out` |
| `review_result` | review | `step`, `tool`, `result_len` |
| `review_selected` | review | `name`, `file`, `detail`, `reason` |
| `review_done` | review | `count`, `tok_in`, `tok_out` |
| `review_forced_done` | review | `reason`, `tok_in`, `tok_out` |
| `reviewer_start` | reviewer | — |
| `reviewer_done` | reviewer | `tok_in`, `tok_out` |
| `reviewer_failed` | reviewer | `reason` |

## Cache

`~/.cache/diffgraph/extractions/<model_slug>/<sha256>.json`

Key: SHA256(file content + model name). Excludes runtime fields (`is_changed`,
`full_code`, `is_expanded`). Written atomically via `.tmp` + rename.
Cache hit emits `cache_hit` event and skips the LLM call entirely.

## Common tasks

**Add a language:**
1. `lang.py`: add to `LANG_MAP`, `DECLARATION_PATTERNS`, `FILE_EXTENSIONS`
2. `renderer.py`: add to `_COMMENT_CHAR` and `_OMIT`

**Change what the LLM extracts:**
Edit `agents/prompts/extract_system.txt`. All prompts use `str.format()` —
escape literal braces as `{{` / `}}`.

**Change agent behaviour:**
Edit the corresponding `agents/prompts/*.txt` file. Prompts are loaded once at
module import time via `agents/prompts/__init__.py:load()`.

**Add a new event:**
Emit `on_event("my_event", **kwargs)` anywhere. Handle it in `cli.py:_make_event_handler`.
No schema — callers ignore unknown events.

**Adjust rendering:**
`renderer.py:_build()` — section structure, what appears per depth.
`renderer.py:_render_compressed()` — partial compression algorithm.
`agents/prompts/render_context.txt` — all section headers and structural text.

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Cover `diff_parser`, `mark_changed_symbols`, and `renderer` without an LLM.
