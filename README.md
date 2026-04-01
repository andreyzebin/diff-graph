# DiffGraph

Lightweight dependency metamodel for code-review agents.

Starts from a `git diff`, extracts entities with an LLM, and recursively walks
dependencies through the repository — producing a compact, structured prompt context
that covers exactly the part of the codebase touched by a PR.

```
raw git diff
     │
     ▼
parse_diff() ──► changed files + changed lines + before-snippets from hunks
     │
     ▼
explore()    ──► BFS: read file → LLM extract → resolve deps → repeat
     │
     ▼
MetaModel    ──► mark_changed_symbols() → before/after code per symbol
     │
     ▼
render()     ──► structured text context for a review agent prompt
```

No pre-indexing. No database. One session, one diff, one metamodel in memory.

---

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the config and add your API key to `.env`:

```bash
cp config.yaml config.local.yaml
echo 'export OPENAI_API_KEY=sk-...' > .env
```

Run:

```bash
source .env
git diff HEAD~1 | python cli.py run --repo . --diff -
```

---

## Configuration

`config.yaml` holds committed defaults with `${VAR}` placeholders.
`config.local.yaml` is deep-merged on top at runtime — gitignored, never committed.
Secrets go in `.env` (also gitignored), sourced before running.

```yaml
# config.local.yaml
llm:
  api_url: "https://api.deepseek.com/v1"   # empty = OpenAI directly
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"

render:
  max_tokens: 8000   # token budget for rendered context

explore:
  depth: 2           # 0 = changed files only, 1 = +direct deps, 2 = +transitive
```

Any setting can be overridden per-run via CLI flags.

---

## CLI

```bash
# Full pipeline — diff file
python cli.py run --repo ./my-service --diff changes.diff

# Full pipeline — pipe from git
git diff HEAD~1 | python cli.py run --repo . --diff -

# Write context to file instead of stdout
python cli.py run --repo . --diff my.diff --output context.txt

# Override model and depth for this run
python cli.py run --repo . --diff my.diff --model gpt-4o --depth 1

# Parse-only — no LLM, verify the diff parser
python cli.py inspect changes.diff
git diff HEAD~1 | python cli.py inspect -
```

**`run` flags:**

| Flag | Description |
|------|-------------|
| `--repo` / `-r` | Path to the repository (after-version checkout) |
| `--diff` / `-d` | Diff file path, or `-` for stdin |
| `--depth` | BFS depth (default: from config) |
| `--model` / `-m` | LLM model name |
| `--api-url` | OpenAI-compatible base URL |
| `--api-key` | API key |
| `--output` / `-o` | Write context to file |

During `run` the CLI streams LLM tokens live: each file being extracted shows a
rolling preview of the token stream so you can see the model thinking in real time.

---

## Python API

```python
from openai import OpenAI
from diffgraph import DiffGraph

client = OpenAI()  # or any OpenAI-compatible client
dg = DiffGraph(repo_path="./my-service", llm_client=client)

# One-shot: diff → prompt context string
context = dg.build_and_render(open("my.diff").read(), depth=2)

# Step-by-step
meta, diff_result = dg.build(open("my.diff").read(), depth=2)
context = dg.render(meta, diff_result)

# With progress callback
def on_event(event, **kw):
    if event == "extracted":
        print(f"{kw['path']}: {kw['symbols']} symbols")

meta, diff_result = dg.build(diff_text, on_event=on_event)
```

---

## Supported languages

Java · Python · TypeScript / TSX · Go · Kotlin · Ruby · C#

---

## Architecture

```
diffgraph/
├── model.py        # Symbol, Module, MetaModel — in-memory graph
├── lang.py         # language detection, declaration search patterns, file extensions
├── tools.py        # list_files, read_file, search_text
├── diff_parser.py  # git diff → DiffResult (hunks, changed lines, before-snippets)
├── extractor.py    # LLM extraction with streaming + 3-attempt JSON retry
├── explorer.py     # BFS over dependency graph
├── renderer.py     # text render with token-budget degradation
└── diffgraph.py    # DiffGraph public API + mark_changed_symbols
```

**Protections against runaway cost and loops:**

| Mechanism | Behaviour |
|-----------|-----------|
| Token guard | `read_file` without range → first 300 lines |
| Max files | `list_files` > 50 results → first 10 |
| Visited set | Prevents cycles and duplicate LLM calls |
| LLM retry | Invalid JSON → up to 2 retries, then skip module |
| Token budget | `render()` degrades depth-2 → depth-1 → names only |
| Skip externals | Unresolved deps (stdlib, third-party) are silently ignored |
