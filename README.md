# DiffGraph

Multi-agent code review assistant. Starts from a git diff (or a Bitbucket Server PR URL),
builds a dependency metamodel with an LLM, finds impacted callers, and produces a
structured prompt context — or posts inline review comments directly to the PR.

```
git diff / PR URL
      │
      ▼
parse_diff()       changed files + changed lines
      │
      ▼
explore()          BFS: read → LLM extract → resolve deps → repeat
      │
      ▼
MetaModel          mark_changed_symbols() → before/after code per symbol
      │
      ├──► impact agent    ReAct loop: find files impacted by the change
      │
      ├──► planner         single LLM call: review strategy hint
      │
      ├──► review agent    ReAct loop: curate the most relevant context
      │
      ├──► render()        structured text context (token-budget aware)
      │
      └──► reviewer        generate + post inline PR comments
```

No pre-indexing. No database. One session per diff.

---

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Copy config and add credentials:

```bash
cp config.yaml config.local.yaml
# edit config.local.yaml — set api_url, api_key, model
```

Run against a local diff:

```bash
git diff HEAD~1 | python cli.py run --repo . --diff -
```

Run against a Bitbucket Server PR (clones automatically):

```bash
source .env   # exports BITBUCKET_SERVER_BEARER_TOKEN
python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42 --review
```

---

## Configuration

`config.yaml` holds committed defaults with `${VAR}` placeholders.
`config.local.yaml` is deep-merged on top — gitignored, never committed.
Secrets go in `.env`, sourced before running.

```yaml
# config.local.yaml
llm:
  api_url: "https://api.deepseek.com/v1"   # empty = OpenAI directly
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"

render:
  max_tokens: 8000        # token budget for rendered context

explore:
  depth: 2                # 0 = changed files only, 1 = +direct deps, 2 = +transitive
  max_callers: 5          # max impacted files to add to the graph
  max_agent_steps: 32     # ReAct step budget for impact + review agents
  max_agent_tokens: 20000 # token budget for impact + review agents
```

Environment variables for Bitbucket Server:

```bash
BITBUCKET_SERVER_BEARER_TOKEN=...   # required
REQUESTS_CA_BUNDLE=/path/to/ca.pem  # optional, custom CA
BITBUCKET_SERVER_CLIENT_CERT=...    # optional, mTLS client cert
```

---

## CLI

```bash
# Local diff
python cli.py run --repo ./my-service --diff changes.diff
git diff HEAD~1 | python cli.py run --repo . --diff -

# Bitbucket Server PR — auto-clone + diff
python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42

# With review agent (curated context instead of BFS render)
python cli.py run --pr-url ... --review

# Generate and post inline comments to the PR
python cli.py run --pr-url ... --review --post-comments

# Write context to file
python cli.py run --repo . --diff my.diff --output context.txt

# Dump the full MetaModel as JSON
python cli.py run --repo . --diff my.diff --dump-graph graph.json

# Parse only — no LLM
python cli.py inspect changes.diff
git diff HEAD~1 | python cli.py inspect -
```

**`run` flags:**

| Flag | Description |
|------|-------------|
| `--repo` / `-r` | Path to repository (after-version checkout) |
| `--diff` / `-d` | Diff file path, or `-` for stdin |
| `--pr-url` | Bitbucket Server PR URL — clones repo and fetches diff automatically |
| `--review` | Run review agent for curated context instead of BFS renderer |
| `--post-comments` | Post review comments to the PR (requires `--pr-url` and `--review`) |
| `--depth` | BFS depth (default: from config) |
| `--model` / `-m` | LLM model override |
| `--api-url` | OpenAI-compatible API base URL override |
| `--api-key` | API key override |
| `--output` / `-o` | Write rendered context to file |
| `--dump-graph` | Write MetaModel as JSON to file |

During `run`, each LLM call streams tokens live with a rolling preview. All agent steps
are permanently logged so the full execution history is visible after completion.

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

# With review agent
meta, diff_result = dg.build(diff_text)
context = dg.review(meta, diff_result, pr_title="...", pr_description="...")

# With progress callback
def on_event(event, **kw):
    if event == "extracted":
        print(f"{kw['path']}: {kw['symbols']} symbols")
    elif event == "agent_step":
        print(f"  step {kw['step']}  {kw['tool']}")

meta, diff_result = dg.build(diff_text, on_event=on_event)
```

---

## Supported languages

Java · Python · TypeScript / TSX · Go · Kotlin · Ruby · C#

---

## Architecture

```
diffgraph/
├── model.py             # Symbol, Module, MetaModel — in-memory graph
├── lang.py              # language detection, file extensions, declaration patterns
├── tools.py             # list_files, read_file, search_text — filesystem primitives
├── diff_parser.py       # git diff text → DiffResult (hunks, changed lines)
├── cache.py             # content-addressed LLM extraction cache (~/.cache/diffgraph/)
├── explorer.py          # explore() BFS + explore_callers() impact analysis
├── renderer.py          # render() with token-budget degradation + partial compression
├── diffgraph.py         # DiffGraph public API + mark_changed_symbols()
├── agents/
│   ├── extractor.py     # extract_module() — one streaming LLM call → Module
│   ├── impact.py        # find_impact() — ReAct agent: find impacted callers
│   ├── review.py        # find_review_context() — ReAct agent: curate context
│   ├── planner.py       # plan_review() — single call: strategy hint for review agent
│   ├── resolver.py      # resolve_dep() — agentic dependency path resolution
│   ├── reviewer.py      # generate_review_comments() — inline comment generation
│   ├── streaming.py     # stream_llm() — shared streaming helper for all agents
│   └── prompts/         # all prompt text files (SECTION: blocks)
└── providers/
    └── bitbucket_server.py   # PR fetch (clone + diff) + comment posting
```

**Cost controls:**

| Mechanism | Behaviour |
|-----------|-----------|
| Extraction cache | SHA256(content+model) → skip LLM if file unchanged |
| `read_file` cap | Without range: first 300 lines; with range: max 100 lines |
| BFS visited set | Prevents cycles and duplicate LLM calls |
| LLM retry | Invalid JSON → up to 2 retries, then skip module |
| Adaptive budget | Agent nudged at 50% and 75% of token budget |
| Token-budget render | Degrades depth-2 → depth-1 → names only if over limit |
| Parallel tool calls | Multiple agent tool calls executed concurrently |
