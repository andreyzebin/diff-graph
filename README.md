# DiffGraph

Multi-agent PR code reviewer powered by the **Orchestra** framework. All agents defined by `.prompt` files — hierarchy, behavior, and data flow controlled entirely by prompts.

## Contents

- [Quickstart](#quickstart)
- [Configuration](#configuration)
  - [Git authentication](#git-authentication)
  - [Corporate TLS](#corporate-tls)
  - [Mutual TLS (client certificate)](#mutual-tls-client-certificate)
- [CLI](#cli)
- [How it works](#how-it-works)
  - [Agents](#agents)
  - [Data flow: from:tool.field](#data-flow-fromtoolfield)
  - [Guards](#guards)
  - [Three-phase review](#three-phase-review-methodology)
- [Orchestra Framework](#orchestra-framework)
- [Architecture](#architecture)
- [Docker](docker/README.md)

```
PR comment / event
      |
      v
+--- dispatcher (react) ----------------+
|  /review → spawn reviewer             |
|  /help   → reply directly             |
|  /ask hi → answer, suggest commands   |
|  plain   → answer from PR context     |
+----------------------------------------+
      |  spawn_agent("reviewer")
      |  (lazy clone on first tool call)
      v
+--- reviewer (react, spawns children) -+
|  Phase 1: ANALYZE — form concerns      |
|  Phase 2: INVESTIGATE — spawn          |
|  Phase 3: JUDGE — consolidate, done    |
|     +-- investigator (react + SGR)     |
|     +-- investigator (react + SGR)     |
+----------------------------------------+
      |
      v
ReviewFinding[]  →  inline PR comments
```

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

Run against a Bitbucket Server PR:

```bash
source .env
python cli.py run --pr-url https://bitbucket.example.com/projects/X/repos/Y/pull-requests/42
```

Run with dispatcher (interactive commands):

```bash
python cli.py run --pr-url ... --message "/review"
python cli.py run --pr-url ... --message "/help" --comment-id 12345
python cli.py run --pr-url ... --message "Is this null-safe?" --comment-id 12345
```

Run against a local repo (direct review, no dispatcher):

```bash
python cli.py run --repo . --base HEAD~1
python cli.py run --repo . --base main --source feature/my-branch
```

---

## Configuration

### `.env`

```bash
# LLM
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...

# Bitbucket Server
BITBUCKET_SERVER_BEARER_TOKEN=...              # Bearer token for API + git clone
REQUESTS_CA_BUNDLE=/path/to/ca.pem            # CA for Bitbucket (optional)
BITBUCKET_SERVER_CLIENT_CERT=/path/to/client.pem  # mTLS client cert (optional)

# Git auth mode
# DIFFGRAPH_GIT_AUTH=ssh                       # Use SSH instead of http.extraHeader
# BITBUCKET_SSH_PORT=7999                      # SSH port (default 7999)
```

### Git authentication

Two modes controlled by `DIFFGRAPH_GIT_AUTH`:

| Mode | Env var | Git method | Best for |
|---|---|---|---|
| `header` (default) | `BITBUCKET_SERVER_BEARER_TOKEN` | `http.extraHeader` with Bearer token | Linux, Docker |
| `ssh` | — | `ssh://git@server:port/...` via ssh-agent | Windows, SSH keys |

### `config.local.yaml`

```yaml
llm:
  api_url: "https://api.deepseek.com/v1"
  api_key: "${DEEPSEEK_API_KEY}"
  model: "deepseek-chat"
  tool_choice: "required"    # "required" (default) or "auto" for models that don't support required
  timeout: 600

review:
  max_steps: 40
  max_tokens: 40000
  bot_user: ""               # Bitbucket slug — own comments marked [SELF]
```

### `tool_choice`

Some LiteLLM-proxied models (e.g. `Qwen3-Coder-480B`) don't support `tool_choice="required"`. Set `tool_choice: "auto"` in `config.local.yaml`. Can also be set per-agent: `@llm: tool_choice=auto`.

### Corporate TLS

DiffGraph uses [truststore](https://pypi.org/project/truststore/) to automatically pick up OS-level CA certificates (corporate VPN, proxy CAs). No manual CA bundle needed in most cases.

If still failing — `--no-verify-ssl` as a quick workaround.

### Mutual TLS (client certificate)

Some corporate Bitbucket instances require a client certificate (mTLS). DiffGraph needs a PEM file.

**Convert P12 to PEM:**

```bash
openssl pkcs12 -in client.p12 -out client.pem -nodes -passin pass:YOUR_PASSWORD
chmod 600 client.pem
```

Then set in `.env`:

```bash
export BITBUCKET_SERVER_CLIENT_CERT=/path/to/client.pem
```

**Find client certificate on Windows:**

1. Open `certmgr.msc` (Win+R → `certmgr.msc`)
2. Go to **Personal → Certificates**
3. Find your corporate certificate (usually issued by your company's CA)
4. Right-click → **All Tasks → Export...**
5. Select **Yes, export the private key**
6. Choose **PKCS #12 (.PFX)** format, set a password
7. Save as `client.p12`
8. Convert to PEM with the command above

**Find client certificate on macOS:**

1. Open **Keychain Access**
2. Category: **My Certificates**
3. Find the corporate certificate, right-click → **Export...**
4. Save as `.p12`, set a password
5. Convert to PEM

**Linux** — client certs are usually at `/etc/pki/tls/certs/` or provided by your admin as `.p12`/`.pem` files.

---

## CLI

### `run` -- review code changes

```bash
# Dispatcher (default with --message)
python cli.py run --pr-url ... --message "/review" --comment-id 12345
python cli.py run --pr-url ... --message "/help" --comment-id 12345

# Direct review (no dispatcher)
python cli.py run --pr-url https://bitbucket.example.com/.../pull-requests/42

# Run any agent by name
python cli.py run --pr-url ... --agent reviewer
python cli.py run --pr-url ... --agent investigator -d focus="null safety"

# Local mode
python cli.py run --repo . --base HEAD~1
```

| Flag | Description |
|------|-------------|
| `--pr-url` | Bitbucket Server PR URL |
| `--message` | User message (`/review`, `/help`, plain text). Runs dispatcher by default. |
| `--comment-id` | Bitbucket comment ID that triggered this invocation |
| `--agent` | Run a specific agent by name (`dispatcher`, `reviewer`, `investigator`) |
| `-d` / `--data` | Data key=value pairs for the agent (e.g. `-d focus="null safety"`) |
| `--repo` / `-r` | Path to local repository (local mode) |
| `--base` | Base ref (commit/branch to merge into) |
| `--source` | Source ref (default: HEAD) |
| `--model` / `-m` | LLM model override |
| `--output` / `-o` | Write findings as JSON |
| `--max-steps` | Max ReAct tool calls |
| `--max-tokens` | Max token budget |
| `--prompts` | Prompt resource URI (path, `file://`, `bitbucket://`) |
| `--log-level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `-v` / `--verbose` | Shortcut for `--log-level DEBUG` |
| `--no-verify-ssl` | Disable SSL verification |

### `trace` -- view execution traces

```bash
python cli.py trace              # open last run in browser
python cli.py trace --log        # print trace to console
python cli.py trace --list       # list recent runs
python cli.py trace --run ID     # specific run
```

### Prompt versioning

```bash
python cli.py run --pr-url ... --prompts /path/to/prompts/v2
python cli.py run --pr-url ... --prompts bitbucket://server/PROJECT/prompts-repo/refs/main/prompts
```

---

## How it works

### Agents

All agents are homogeneous — same `Agent` class, same `.prompt` format, same tool dispatch. Hierarchy and behavior controlled entirely by prompts.

**Dispatcher** — entry point for user interactions. Handles `/help`, questions, and planned commands directly. Only spawns reviewer on explicit `/review` command or auto-trigger. Uses `@guards` to ensure replies are delivered via tools.

**Reviewer** — conducts the code review. Three phases: analyze (read diff, form concerns), investigate (spawn investigators), judge (consolidate findings). Owns PR comment interaction.

**Investigator** — focused agent with SGR. Gets a concern as focus, investigates with repo tools (read_file, search, read_outline). Returns findings with evidence.

### Data flow: `from:tool.field`

Agents declare data dependencies in `@data`. Missing fields are auto-resolved from cached data-provider tools:

```
@data:
  diff_summary: string -- from:pr_context.diff_summary
  focus: string -- task from parent
```

When investigator is spawned without `diff_summary`, the framework calls `pr_context()` tool (cached, hidden), extracts `.diff_summary`, injects into prompt. One tool call serves all fields. No domain code in the framework.

### Guards

`@guards` configure automatic interventions when agent behavior goes wrong:

```
@guards:
  text_response: "Your text was NOT delivered. Use reply_to_comment()."
  require_tool:reply_to_comment: "You must reply before finishing."
```

- `text_response` — model returned text without tool calls. Message injected, loop continues (max 2 retries).
- `require_tool:X` — model called `done()` without calling tool X. Done cancelled, message injected, loop continues.

### Lazy clone

Repo clone + diff only happen when a domain tool is first called (`ctx.ensure_repo()`). `/help` and plain questions skip clone entirely.

### Three-phase review methodology

**Phase 1 -- ANALYZE:** Read the diff, identify concerns scaled to diff size (1-2 small, 3-5 large). Each concern is a distinct theme.

**Phase 2 -- INVESTIGATE (one round):** Spawn investigator(s) — one per concern. Investigators use repo tools + SGR to track reasoning. One spawn round, no iteration.

**Phase 3 -- JUDGE:** Resolve concerns from evidence, handle existing PR comments, deduplicate findings, deliver verdict.

### SGR (Self-Guided Reasoning)

Every react agent tracks reasoning via `reflect()`: `learned`, `questions_remaining`, `resolved_questions`, `confidence`, `next_action`. Question IDs provide stability across reflects.

### CLI output

```
10:07:04 INFO reviewer: read_file(path=…/OrderService.java, changes_only=True) → 47 lines
10:07:06 INFO reviewer: reflect  medium
10:07:07 INFO spawn investigator → BUSINESS LOGIC: Investigate...
10:07:09 INFO investigator: read_file(path=…/OrderService.java) → 120 lines
10:07:10 INFO investigator: search(query=getItems) → 12 lines
10:07:12 INFO investigator: reflect  high
10:07:14 INFO investigator: done
10:07:15 INFO reviewer: reflect  high
10:07:17 INFO reviewer: resolve_comment(comment_id=1149607)
10:07:19 INFO done: 1 findings, 0 replies, 4 resolves
```

---

## Orchestra Framework

Prompt-defined agent framework (~3,700 LOC). Agents defined by `.prompt` files with `@` headers. No topologies, no pipelines — agents create structure at runtime via tool calls.

### Prompt file format

```
@agent: investigator
@mode: react
@capabilities: sgr
@tools: find_files, read_file, search
@budget: 15000 tokens, 20 steps
@llm: temperature=0
@guards:
  text_response: "Use tools to investigate, don't just return text."
@data:
  diff_summary: string -- from:pr_context.diff_summary
  focus: string -- specific concern to investigate
@summary: Investigates one aspect of a PR with tools and SGR.
---
You are investigating a specific concern in a code review.
{diff_summary}
YOUR TASK: {focus}
```

### Key features

| Feature | Description |
|---|---|
| `@data` + `from:tool.field` | Auto-resolve prompt data from cached tool calls |
| `@guards` | Reactive guards: `text_response`, `require_tool:X` |
| `@capabilities: spawn` | Agent can spawn children via `spawn_agent`, `spawn_many` |
| JSON Schema validation | All tool calls validated before dispatch (jsonschema) |
| Trace system | SQLite WAL, live WebSocket view, navigator with per-step detail |
| SGR | Self-Guided Reasoning with question IDs and fuzzy matching |
| Budget + pushers | Token/step limits with configurable nudge/force_done thresholds |
| Mutable LLM params | Parent can `adjust_agent` child's temperature, model, etc. |

### Tool system

All tools — domain and builtin — go through `registry.dispatch()`. Schema validation, caching, hidden data providers. Tools registered with `cache=True, hidden=True` serve as data providers for `from:` resolution.

---

## Architecture

```
orchestra/                   Prompt-defined agent framework
+-- agent.py                 Agent + resolve_agent_data()
+-- compiler.py              .prompt → agent registry (regex + LLM fallback)
+-- tools/
    +-- registry.py          dispatch, validation, cache, hidden
    +-- builtin.py           Meta-tools with real agent handlers
+-- types.py                 AgentConfig (guards, input_schema, ...)
+-- events.py                EventBus
+-- budget.py                BudgetState + pushers
+-- sgr.py                   SGR with question IDs
+-- trace.py                 Trace collection
+-- trace_db.py              SQLite storage + reader
+-- streaming.py             LLM streaming
+-- handoff.py               Context handoff modes
+-- condensation.py          Message condensation strategies
+-- feedback.py              Behavioral signals
+-- merge.py                 Merge strategies
+-- prompts.py               Template interpolation

diffgraph/                   Code review domain
+-- orchestrator.py          run_agent() + run_review()
+-- orchestra_tools.py       Domain tools + pr_context data provider
+-- api.py                   DiffGraph public API
+-- diff_parser.py           git diff → DiffResult
+-- bitbucket.py             Bitbucket Server integration
+-- providers/
    +-- bitbucket_pr.py      Bitbucket REST API
    +-- git_repo.py          Git clone/fetch/diff (header | ssh)
+-- prompts/
    +-- dispatcher.prompt    Route commands, handle /help, spawn reviewer
    +-- reviewer.prompt      Three-phase review (analyze → investigate → judge)
    +-- investigator.prompt  Focused investigation with SGR

diffsearch/                  Virtual unified diff filesystem
webhook/                     Bitbucket webhook router with A/B routing
tracing/                     Trace web server (FastAPI + Alpine.js)
evolution/                   Self-sustaining prompt development
docker/                      Dockerfile + entrypoint
```

## Tests

```bash
source .venv/bin/activate
pytest
```
