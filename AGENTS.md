# Agent Guide

This document describes the codebase for AI agents and coding assistants.

## What this project does

DiffGraph builds a lightweight in-memory metamodel of the code touched by a PR.
It takes a raw `git diff`, extracts entities from the changed files using an LLM,
then recursively resolves dependencies via BFS — reading only the files that are
actually relevant to this diff. The result is a structured text context designed
to be injected into a code-review agent's prompt.

No pre-indexing, no database, no persistent state. One `DiffGraph.build()` call
per review session.

## Repository layout

```
diffgraph/
├── model.py        # Symbol, Module, MetaModel dataclasses — the in-memory graph
├── lang.py         # language detection + search patterns per language
├── tools.py        # list_files, read_file, search_text — filesystem primitives
├── diff_parser.py  # parse_diff() — git diff text → DiffResult
├── extractor.py    # extract_module() — one LLM call → Module
├── explorer.py     # explore() — BFS, resolve_dependency()
├── renderer.py     # render() — MetaModel → text prompt context
└── diffgraph.py    # DiffGraph class + mark_changed_symbols()
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
        ├─ changed_files       → start nodes for BFS
        ├─ changed_lines       → which after-lines were touched
        └─ files[*].hunks      → before_lines per hunk (for before_code)

explore(start_files, repo_path, llm, max_depth)
  └─► MetaModel
        └─ modules: {path → Module}
              └─ Module.symbols: [Symbol(start_line, end_line, ...)]

mark_changed_symbols(model, diff_result, repo_path)
  └─► for each changed file:
        match Symbol.start_line..end_line against after_changed_lines
        symbol.is_changed = True
        symbol.full_code  = read_file(after-version)
        symbol.before_code = collected from hunk.before_lines

render(model, diff_result)
  └─► text context string
```

## Key abstractions

### `DiffResult` (`diff_parser.py`)

Output of `parse_diff()`. Three things matter downstream:

- `changed_files` — list of after-paths for BFS start nodes (excludes deleted files)
- `changed_lines` — `{path: [line_numbers]}` of `+` lines in the after-version
- `files[path].hunks` — list of `HunkSnippet` with `before_lines` / `after_lines`

`HunkSnippet.after_start` is the line number of the first hunk line (including context)
in the after-version. `after_lines` contains only the `+` lines (no context).

### `MetaModel` (`model.py`)

Single flat dict: `modules: {path → Module}`. No before/after split — the graph
is always the after-version. Before-code lives only in `Symbol.before_code` for
symbols that were changed.

`Module.depth` records at which BFS depth the module was found (0 = directly changed,
1 = direct dependency, 2 = transitive).

### `extract_module` (`extractor.py`)

One LLM call per file. Returns a `Module` with `symbols` (each has `start_line`,
`end_line`, `kind`, `signature`, `summary`) and `dependencies` (names only — not paths).

Uses `stream=True`. Fires `on_event("token", text=accumulated)` for each chunk so
callers can show a live preview. Retries up to 2 times on invalid JSON before
returning `None` (graceful degradation — the BFS continues without this module).

The prompt (`EXTRACT_PROMPT`) uses `str.format()` — all literal `{` / `}` in the
template must be doubled as `{{` / `}}`.

### `resolve_dependency` (`explorer.py`)

Two-step lookup:
1. `list_files(f"**/{name}{ext}")` for each extension in the language's `FILE_EXTENSIONS`
2. `search_text(pattern)` for each pattern in `DECLARATION_PATTERNS`

Returns `None` for anything that looks like a third-party library or stdlib name.
`_best_match()` picks the shortest path when multiple files match (monorepo heuristic).

### `mark_changed_symbols` (`diffgraph.py`)

Runs after `explore()`. Matches `Symbol.start_line..end_line` against
`diff_result.changed_lines[path]` (the set of `+` line numbers).

`_extract_before_code()` collects `hunk.before_lines` from all hunks whose
after-range intersects the symbol's line range. This gives a "what was removed/changed"
view without needing a git checkout of the before-version.

**Known limitation:** pure deletions (hunks with no `+` lines) produce no entries in
`after_changed_lines`, so a symbol that only lost lines will not be marked as changed.

### `render` (`renderer.py`)

Detail by depth:

| Scope | Content |
|-------|---------|
| Changed symbol | `before_code` + `full_code` (after) + signature + summary |
| depth 0, unchanged | signature + summary |
| depth 1 | all symbols: signature + summary |
| depth 2 | module summary only |

Token budget (`len(text) // 4`): if over `max_tokens`, degrades depth-2 to names,
then depth-1 to summary-only. Changed modules are never truncated.

### `on_event` callback

`explore()`, `extract_module()`, and `DiffGraph.build()` all accept an optional
`on_event(event: str, **kwargs)` callback. Events:

| Event | Key kwargs |
|-------|-----------|
| `reading` | `path`, `depth` |
| `extracting` | `path`, `model`, `attempt` |
| `token` | `text` (accumulated stream so far) |
| `extracted` | `path`, `symbols` (count), `deps` (list of names) |
| `retry` | `path`, `attempt`, `reason` |
| `failed` | `path` |
| `read_failed` | `path` |
| `resolving` | `name` |
| `resolved` | `name`, `path` |
| `not_resolved` | `name` |

## Configuration

`config.yaml` holds committed defaults. `config.local.yaml` (gitignored) is
deep-merged on top. All string values support `${ENV_VAR}` expansion via
`_expand_config()` in `cli.py`.

```yaml
llm:
  api_url: ""                    # empty = OpenAI; any OpenAI-compatible URL otherwise
  api_key: "${OPENAI_API_KEY}"
  model: "gpt-4o-mini"

render:
  max_tokens: 8000

explore:
  depth: 2
```

Secrets go in `.env` (gitignored), sourced before running.

## CLI commands

```bash
# Full pipeline — streams token output during extraction
python cli.py run --repo ./my-service --diff changes.diff
git diff HEAD~1 | python cli.py run --repo . --diff -

# Parser only — no LLM, useful for debugging diff parsing
python cli.py inspect changes.diff
git diff HEAD~1 | python cli.py inspect -
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Tests cover `diff_parser`, `mark_changed_symbols`, and `renderer` without an LLM.
`extractor` and `explorer` are tested via simple fake LLM stubs in the test files.

## Common tasks

**Add a language:**
1. Add extension → language mapping in `lang.py:LANG_MAP`
2. Add declaration patterns in `DECLARATION_PATTERNS`
3. Add file extensions in `FILE_EXTENSIONS`

**Change what the LLM extracts:**
Edit `EXTRACT_PROMPT` in `extractor.py`. Remember to escape all `{` / `}` not used
for `.format()` substitution as `{{` / `}}`.

**Add a new event type:**
Emit `on_event("my_event", **kwargs)` anywhere in `explorer.py` or `extractor.py`.
Handle it in `cli.py:_make_event_handler`. No schema — callers ignore unknown events.

**Adjust rendering detail:**
`renderer.py:render()` — the three-tier degradation logic is self-contained.
`renderer.py:_render_changed_module()` — controls what appears for each changed symbol.
