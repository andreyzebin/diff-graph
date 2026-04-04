# DiffGraph

Agentic PR code reviewer. Takes a git diff (or a Bitbucket Server PR URL) and runs a two-phase single-agent review: a strategist plans what to look for, then a ReAct agent explores the repo and produces structured inline findings.

```
git diff / PR URL
      │
      ▼
parse_diff()       changed files + changed lines
      │
      ▼
plan phase         one LLM call: detect system type, build typed task list
      │
      ▼
solve phase        ReAct loop: explore repo with tools, reflect, report
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
| `reflect(learned, questions_remaining, confidence, next_action)` | SGR structured self-reflection |
| `done(findings)` | Submit findings and stop |

Multiple tool calls from a single LLM response execute in parallel via `ThreadPoolExecutor`.

Adaptive budget nudges at 50% and 75% of `max_tokens` push the agent toward wrapping up.

### SGR (Self-Guided Reasoning)

The agent calls `reflect()` every 3–5 steps to track what it has learned, what questions remain open, and what to do next. `reflect()` always returns `"Reflection noted."` — its value is purely in structuring the agent's reasoning before the next step.

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
diffgraph/
├── api.py               DiffGraph public API
├── diff_parser.py       git diff text → DiffResult (hunks, changed lines)
├── lang.py              language detection
├── tools.py             list_files, read_file, search_text
├── outline.py           tree-sitter structural outline
├── streaming.py         stream_llm() helper
├── orchestrator.py      run_review(): plan phase + ReAct solve phase
├── bitbucket.py         fetch_pr, post/get/reply/resolve PR comments
└── prompts/
    ├── strategist_system.txt    plan phase prompt
    └── orchestrator_system.txt  ReAct + SGR prompt
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```
