# DiffGraph

Multi-agent PR code reviewer powered by the **Orchestra** framework. All agents defined by Markdown files with YAML frontmatter (`<name>.md` + sibling `<name>.system.md` / `<name>.user.md`) — hierarchy, behavior, and data flow controlled entirely by prompts.

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
- [Running as systemd services on RHEL](#running-as-systemd-services-on-rhel)
- [Docker](docker/README.md)
- [Webhook router & health checks](webhook/README.md)
- [Quality management architecture](docs/qa-architecture.md) — how bench (unit + integration tiers) and pr-analytics (merge_acceptance_rate) form a closed improvement loop

### Three commands, three roles

The dispatcher supports exactly three commands. Each has a distinct
intent and is shaped by the active thread when invoked from a comment.

| Command   | Role                | Thread-aware behaviour                                                                                                                        |
|-----------|---------------------|------------------------------------------------------------------------------------------------------------------------------------------------|
| `/ask`    | **discussion**      | Answer questions, talk through code and diff, scoped to the active thread's topic. Sibling threads are background, not the subject of the reply. Plain text without a slash falls into this mode. |
| `/help`   | **interface help**  | Explain how to work with the agent — what commands exist, how to summon it, and which command best fits the user's current situation given the active thread. Not a static command list. |
| `/review` | **deep analysis**   | Full code-review pipeline. When invoked from a thread, focuses on that thread's topic but also reads sibling threads + author attribution to strengthen findings (cite prior debate, avoid re-raising resolved points, respect prior speakers). |

All three see the same thread graph: the active thread rendered fully
with `[SELF]` markers on the agent's own past replies and `[<name>]`
attribution per speaker; sibling threads as one-line summaries. None
of them conflate authors — each speaker is a separate subjective
position.

```
PR comment / event
      |
      v
+--- dispatcher (react) ----------------+
|  /ask    → discussion (thread-scoped) |
|  /help   → interface help             |
|  /review → spawn reviewer             |
|  plain   → treated as /ask            |
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
| `--provider` | LLM provider profile from `~/repos/.llm_creds.toml` (e.g. `deepseek`, `qwen3-6`) |
| `--model` / `-m` | LLM model override (overrides provider's `model`) |
| `--api-url` / `--api-key` | Endpoint overrides |
| `--trace-dir` | Mirror traces to a filesystem layout (in addition to SQLite) |
| `--bot-user` | Bitbucket slug of the bot account; own comments are tagged `[SELF]`. Overrides `$BOT_USER` and `review.bot_user`. |
| `--comment-tag` | Prefix for the traceability footer appended to every posted comment (`<prefix>:<gen>:<mutation>:<run>`). Empty string disables. Overrides `review.comment_tag` (default: `dg`). |
| `--output` / `-o` | Write findings as JSON |
| `--max-steps` | Max ReAct tool calls |
| `--max-tokens` | Max token budget |
| `--prompts` | Prompt resource URI (path, `file://`, `bitbucket://`) |
| `--log-level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `-v` / `--verbose` | Shortcut for `--log-level DEBUG` |
| `--no-verify-ssl` | Disable SSL verification |

### LLM provider profiles

Profiles live in `~/repos/.llm_creds.toml` (see `.llm_creds.toml.example`).
Each section declares one endpoint and its quirks:

```toml
[providers.deepseek]
base_url    = "https://api.deepseek.com/v1"
api_key     = "${DEEPSEEK_API_KEY}"
model       = "deepseek-chat"
tool_choice = "required"

[providers.qwen3-6]
base_url    = "https://<id>.modelrun.inference.cloud.ru/v1"
api_key     = "${CLOUD_RU_QWEN3_6_API_KEY}"
model       = "<model-name>"
tool_choice = "auto"
extra_body  = { chat_template_kwargs = { enable_thinking = false } }
```

`extra_body` is forwarded to the OpenAI client and used for vendor knobs.
For Qwen3-Coder on vLLM, `enable_thinking=false` is required: without
it the qwen3 tool parser leaves `</parameter>` XML fragments in JSON
arguments and tool calls fail.

Override precedence: CLI flag > provider profile > `config.local.yaml` > `config.yaml`.

### Filesystem trace mirror

Pass `--trace-dir <base>` (or set `DIFFGRAPH_TRACE_DIR`) to dump every
LLM and tool API call to disk alongside the SQLite store. Layout:

```
<base>/runs/<run-id>/
  run.json
  events.jsonl
  agents/<name>-N/
    meta.json
    step-NN-request.json            # LLM request (messages, tools, params)
    step-NN-response.json           # LLM response
    step-NN-tool-SS-request.json    # tool request
    step-NN-tool-SS-response.json   # tool response
    artifacts/                      # free-form via agent.dump_artifact()
```

Files are written write-ahead with atomic rename, so a crash mid-step
still leaves the request payload on disk for inspection.

Set `DIFFGRAPH_TRACE_PATH=<exact-dir>` instead when a parent runner
(e.g. the benchmark) dictates the target path.

### `trace` -- view execution traces

```bash
python cli.py trace              # open last run in browser
python cli.py trace --log        # print trace to console
python cli.py trace --list       # list recent runs
python cli.py trace --run ID     # specific run
```

### `health` -- ping an LLM endpoint

A single tiny chat completion. Useful for keeping rented-GPU vLLM
nodes (cloud.ru, etc.) warm — they often suspend after ~30 min idle
and take 10–15 min to cold-start, so the first real PR comment
otherwise pays that latency end-to-end.

```bash
python cli.py health --provider qwen3-6
python cli.py health --provider deepseek -q && echo "alive"
```

| Flag | Description |
|------|-------------|
| `--provider` | LLM profile from `~/repos/.llm_creds.toml` |
| `--model` / `-m` | Model override |
| `--api-url` / `--api-key` | Endpoint overrides |
| `--timeout` | Request timeout (default 1200s — covers cold start) |
| `-q` / `--quiet` | Suppress output, just exit code |
| `--no-verify-ssl` | Disable SSL verification |

The webhook router can run this on a schedule via `[[health]]` —
see [webhook/README.md](webhook/README.md#health-checks).

### Prompt versioning

```bash
python cli.py run --pr-url ... --prompts /path/to/prompts/v2
python cli.py run --pr-url ... --prompts bitbucket://server/PROJECT/prompts-repo/refs/main/prompts
```

---

## How it works

### Agents

All agents are homogeneous — same `Agent` class, same Markdown+YAML-frontmatter format, same tool dispatch. Hierarchy and behavior controlled entirely by prompts.

Each agent is described by three sibling files:
- `<name>.md` — YAML frontmatter (metadata: `agent`, `tools`, `budget`, `data`, `summary`)
- `<name>.system.md` — stable methodology, tool docs, severity rules, finding shape (no per-call placeholders → cacheable)
- `<name>.user.md` — per-call task template with `{placeholder}` interpolation (the "what to do this run")

System and user templates follow SOLID separation: system declares **capabilities** (closed for modification), user dictates **the task** (open for extension via different user-message variants — see agent-isolation tests below).

**Dispatcher** — entry point for user interactions. Three commands with distinct roles: `/ask` (or plain text) is *discussion* — answers questions scoped to the active thread; `/help` is *interface help* — explains the commands and recommends the right one for the user's current thread state; `/review` is *deep analysis* — spawns the reviewer. The dispatcher only spawns the reviewer on the literal `/review` command or auto-trigger; questions about review do not. Uses `@guards` to ensure replies are delivered via tools.

**Reviewer** — conducts the code review. Three phases: analyze (read diff, form concerns), investigate (spawn investigators), judge (consolidate findings). Owns PR comment interaction. When triggered inside a thread, the reviewer focuses its analysis on the thread's topic but also reads sibling threads with author attribution intact, so findings can cite prior debate, avoid duplicating already-resolved points, and respect what each speaker (including the agent's own `[SELF]` past comments) has previously argued.

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

Prompt-defined agent framework. Agents defined by Markdown files with YAML frontmatter (`<name>.md`) plus sibling `<name>.system.md` / `<name>.user.md` body files. No topologies, no pipelines — agents create structure at runtime via tool calls.

**Agent isolation for testing.** `--mocks <fixture.yaml>` short-circuits any `spawn_agent` / `read_file` / etc. tool call with a canned response (Mockito-style). `--user-message-from <file>` overrides the agent's default user template, so the same reviewer can be tested against different task framings (concerns-only, consolidation-only, full pipeline) without changing the system prompt. `--invocations-out <path>` captures every tool call to a JSON file for the test judge to verify against. See `orchestra/tool_mocks.py` for the fixture format.

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
+-- compiler.py              .md (YAML frontmatter) → agent registry
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
    +-- dispatcher.md          YAML frontmatter (metadata)
    +-- dispatcher.system.md   Routing rules + thread/SELF awareness
    +-- dispatcher.user.md     Per-call template ({comment_thread}, {message}, …)
    +-- reviewer.md            YAML frontmatter
    +-- reviewer.system.md     Tools as capabilities + severity + AGENTS.md rule
    +-- reviewer.user.md       Default task: end-to-end review
    +-- investigator.md        YAML frontmatter
    +-- investigator.system.md Tools + reflect rules + finding shape
    +-- investigator.user.md   Default task: investigate one focused concern

diffsearch/                  Virtual unified diff filesystem
webhook/                     Bitbucket webhook router with A/B routing
tracing/                     Trace web server (FastAPI + Alpine.js)
evolution/                   Self-sustaining prompt development
docker/                      Dockerfile + entrypoint
```

## Running as systemd services on RHEL

Two daemons ship with DiffGraph:

- **Webhook router** — `python -m webhook --config webhook.toml` (default port `8000`)
- **Trace server** — FastAPI UI over `~/.diffgraph/traces.db` (default port `8080`). Hosts both the per-run trace viewer (`/`, `/runs/{id}`) and the **Quality API** (`/qa/*`) for scheduled cross-mutation evaluation: configurable schedules with tag-filtered scenarios, plan/queue with worker-pool supervisor, per-mutation scoring (hard skill / soft skill / methodology), on-demand fire from `/qa/mutations`, plan cancel.

Systemd unit templates + install/reload helpers live under `scripts/`.

### Prerequisites

Clone the repo, create the venv, install deps, and fill in configs as usual:

```bash
cd /opt/diffgraph                            # or wherever you checked out the repo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                         # edit: API keys, tokens, CA bundle
cp webhook/config.example.toml webhook.toml  # edit: routes, agents
cp config.yaml config.local.yaml             # edit: LLM api_url / model
```

`.env` with `export KEY=value` lines works as-is — the units source it via `bash -lc 'set -a && source .env && exec ...'`.

### Install

```bash
sudo ./scripts/install-services.sh
# or, to install as a dedicated user that owns the checkout:
sudo INSTALL_USER=diffgraph ./scripts/install-services.sh
```

The installer:

1. Substitutes `__INSTALL_DIR__` / `__USER__` in the unit templates.
2. Writes `diffgraph-webhook.service` and `diffgraph-trace.service` to `/etc/systemd/system/`.
3. `systemctl daemon-reload && systemctl enable --now` both units.

Ports are overridable via `.env`: `export WEBHOOK_PORT=8000` / `export TRACE_PORT=8080`.

SELinux: if the repo lives outside a standard location (`/home/...`, `/opt/...`), you may need `chcon -R -t bin_t .venv/bin` or run `semanage fcontext` — check `journalctl` for `avc: denied`.

Firewall (firewalld):

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

### Manage

```bash
sudo systemctl status  diffgraph-webhook diffgraph-trace
sudo systemctl restart diffgraph-webhook diffgraph-trace
sudo systemctl stop    diffgraph-webhook diffgraph-trace
sudo systemctl disable diffgraph-webhook diffgraph-trace   # stop auto-start on boot
```

### Logs

Both services log to the systemd journal under their `SyslogIdentifier`:

```bash
# Follow live
journalctl -u diffgraph-webhook -f
journalctl -u diffgraph-trace -f

# Last N lines
journalctl -u diffgraph-webhook -n 200 --no-pager

# Since a time window
journalctl -u diffgraph-webhook --since "1 hour ago"
journalctl -u diffgraph-webhook --since today

# Errors only
journalctl -u diffgraph-webhook -p err

# Both at once, live
journalctl -u diffgraph-webhook -u diffgraph-trace -f
```

### Reload after `git pull` or config change

```bash
# After pulling new sources (runs: git pull → pip install -r requirements.txt → restart both)
./scripts/reload-services.sh

# After editing only webhook.toml / config.local.yaml / .env (no code pull, no pip)
./scripts/reload-services.sh --no-pull --no-pip

# Restart only one service
./scripts/reload-services.sh --no-pull --no-pip webhook
./scripts/reload-services.sh --no-pull --no-pip trace
```

Run the reload script as the checkout's owner (NOT root) — it needs `git pull` rights on the working tree and uses `sudo systemctl restart` only for the final step.

### Updating the unit files themselves

If you edit anything under `scripts/*.service`, rerun the installer to push the new version to `/etc/systemd/system/`:

```bash
sudo ./scripts/install-services.sh
```

`daemon-reload` happens automatically.

---

## Tests

```bash
source .venv/bin/activate
pytest
```
