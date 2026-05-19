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
  - [Replay tier — record real PRs](#replay-tier--record-real-prs-replay-against-the-current-agent)
- [Orchestra Framework](#orchestra-framework)
- [Architecture](#architecture)
- [Running as systemd services on RHEL](#running-as-systemd-services-on-rhel)
- [Docker](docker/README.md)
- [Webhook router & health checks](webhook/README.md)
- [Benchmark — scenarios & judge](benchmarks/README.md) — the in-repo `benchmarks/` subtree (was `code-review-benchmarks`)
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
      |  agent_spawn("reviewer")
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

cp .llm_creds.toml.example .llm_creds.toml
# edit .llm_creds.toml -- declare LLM provider profiles (gitignored)

cp config.yaml config.local.yaml
# edit config.local.yaml -- set api_url and model if not using OpenAI
```

`.llm_creds.toml` at the repo root is the recommended local profiles
file — it's gitignored and the loader picks it up automatically (it
walks up from the cwd). See [LLM provider profiles](#llm-provider-profiles).

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

# Replay tier (optional — see "Replay tier" section)
# DIFFGRAPH_RECORDINGS_DIR=/home/andrey/eden/diffgraph-recordings   # qa-server root
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

Some LiteLLM-proxied models (e.g. `Qwen3-Coder-480B`) don't support `tool_choice="required"`. Set `tool_choice: "auto"` in `config.local.yaml`. Can also be set per-agent in the prompt's frontmatter: `llm: {tool_choice: auto}`.

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

LLM endpoints are declared as named **profiles** in a `.llm_creds.toml`
file, selected per-run with `--provider <name>`.

**Create your local, non-committed profiles file** — copy the tracked
example to the repo root:

```bash
cp .llm_creds.toml.example .llm_creds.toml   # gitignored — never committed
# then edit .llm_creds.toml with your endpoints
```

The loader (`diffgraph/llm_creds.py`) resolves the file in this order,
first hit wins:

1. `$LLM_CREDS_FILE` — explicit override
2. `.llm_creds.toml` in the current dir, **walking up to the
   filesystem root** — so a copy at the repo root is picked up
   automatically whenever you run from inside the checkout
3. `~/.llm_creds.toml`
4. `~/repos/.llm_creds.toml`

The in-repo copy (#2) is the recommended spot: it's gitignored, lives
next to the code, and works for both the agent and the `benchmarks/`
subtree without any env var. Secrets still don't live in the file —
string values go through `${VAR}` env-var expansion at load time, so
the actual keys stay in `.env`.

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

All agents are homogeneous — same `Agent` class, same `<name>.system.md` + `<name>.user.md` layout (see [Prompt architecture](#prompt-architecture--layered-extension-friendly)), same tool dispatch. Hierarchy and behavior controlled entirely by prompts.

**Dispatcher** — entry point for user interactions. Three commands with distinct roles: `/ask` (or plain text) is *discussion* — answers questions scoped to the active thread; `/help` is *interface help* — explains the commands and recommends the right one for the user's current thread state; `/review` is *deep analysis* — spawns the reviewer. The dispatcher only spawns the reviewer on the literal `/review` command or auto-trigger; questions about review do not. Uses `guards:` to ensure replies are delivered via tools.

**Reviewer** — conducts the code review. Three phases: analyze (read diff, form concerns), investigate (spawn investigators), judge (consolidate findings). Owns PR comment interaction. When triggered inside a thread, the reviewer focuses its analysis on the thread's topic but also reads sibling threads with author attribution intact, so findings can cite prior debate, avoid duplicating already-resolved points, and respect what each speaker (including the agent's own `[SELF]` past comments) has previously argued.

**Investigator** — focused agent with SGR. Gets a concern as focus, investigates with repo tools (read_file, search, read_outline). Returns findings with evidence.

### Data flow: `from:tool.field`

Agents declare data dependencies in `data:`. Missing fields are auto-resolved from cached data-provider tools:

```yaml
data:
  diff_summary:
    type: string
    from: pr_context.diff_summary
  focus:
    type: string
    description: "task from parent"
```

When investigator is spawned without `diff_summary`, the framework calls `pr_context()` tool (cached, hidden), extracts `.diff_summary`, injects into prompt. One tool call serves all fields. No domain code in the framework.

### Guards

`guards:` configure automatic interventions when agent behavior goes wrong:

```yaml
guards:
  text_response: "Your text was NOT delivered. Use pr_post_comment()."
  require_tool:pr_post_comment: "You must reply before finishing."
```

- `text_response` — model returned text without tool calls. Message injected, loop continues (max 2 retries).
- `require_tool:X` — model called `done()` without calling tool X. Done cancelled, message injected, loop continues.

### Pusher pipeline

Per-step middleware chain that nudges the agent toward progress. Two phases share one `StepContext`:

1. **Phase 1 — `apply(ctx)`** runs before every LLM call. Producers (`ReflectCadenceCounter`, `RatioPusher`, `TokenBudgetPusher`, `TimeBudgetPusher`, `ReflectCadencePusher`) read state and write `PusherAction`s onto `ctx.actions`; consumers (`ApplyActionsHandler`, `TracingHandler`) translate actions into `ctx.messages` / `ctx.current_tools` mutations + `BUDGET_THRESHOLD_HIT` events. The two ratio-escalation pushers (token + time) each fire NUDGE → FORCE_REFLECT → FORCE_DONE at 0.5 / 0.75 / 1.0 on their own dimension — the model sees which axis is pressuring it (token vs wall-clock) without conflating signals.
2. **Phase 2 — `on_step_done(ctx)`** runs after the LLM's tools dispatch. Stateful handlers (e.g. `ReflectCadenceCounter`) inspect `ctx.step_outcomes` (the names + `is_error` flags of every tool that ran) and update internal state for the next step.

This split keeps the agent loop free of per-tool counter mechanics — `reflect` flows through `registry.dispatch` like every other tool, validation rejects malformed args, and `ReflectCadenceCounter.on_step_done` decides whether to reset the cadence counter based on whether reflect actually ran (validation passed) or merely tried (validation rejected, model sees the error in its tool_result and can self-correct).

Per-prompt config: `reflect_interval` (step cadence) and optional wall-time budget (drives `TimeBudgetPusher` 0.5 / 0.75 / 1.0 → NUDGE / FORCE_REFLECT / FORCE_DONE). See `orchestra/budget.py` for the chain shape.

### Lazy clone

Repo clone + diff only happen when a domain tool is first called (`ctx.ensure_repo()`). `/help` and plain questions skip clone entirely.

### Three-phase review methodology

**Phase 1 -- ANALYZE:** Read the diff, identify concerns scaled to diff size (1-2 small, 3-5 large). Each concern is a distinct theme.

**Phase 2 -- INVESTIGATE (one round):** Spawn investigator(s) — one per concern. Investigators use repo tools + SGR to track reasoning. One spawn round, no iteration.

**Phase 3 -- JUDGE:** Resolve concerns from evidence, handle existing PR comments, deduplicate findings, deliver verdict.

### SGR (Self-Guided Reasoning)

`reflect()` is a **convergence aid for investigative multi-step problems** — not a logging tool. Each call externalises the agent's working state so it survives the next step's prompt rebuild instead of living only in working memory, where it gets crowded out as context grows.

The five fields each defend against a specific failure mode of long LLM chains:

- `learned` — anchors established facts as plain text the next step's prompt sees verbatim. Defends against **drift** (partial findings vanishing as context grows).
- `questions_remaining` (with stable IDs `Q1`/`Q2`/…) — what the agent still needs to answer to make the NEXT decision. Defends against **loops** (re-asking what was already resolved): later reflects close by ID, not by re-typing prose.
- `resolved_questions` — references previous IDs with concrete answers. Defends against **premature termination**: the ratio of closed-vs-still-open over successive reflects tells the budget layer whether the agent is converging or spinning.
- `confidence` (low/medium/high) — defends against **mis-calibration**. Drift between confidence and questions_remaining (e.g. `high` with three load-bearing questions still open) is a smell the judge flags as `wrong-reasoning`.
- `next_action` — one concrete step, justified against `learned`. Defends against **unjustified branch switches**.

Reflect lives in the skill layer (`orchestra/skills/reflect.md`), not the default builtins, so single-step responders and mechanical pipelines don't carry the cognitive overhead. Production reviewer / investigator / dispatcher mount it via `skills: [reflect]`. The cadence pusher (`ReflectCadencePusher`) only nudges when reflect is in the agent's tool surface — non-reflective agents see no pressure. See `docs/orchestra-architecture.md` for the full conceptual fix.

### Skills — composable bundles of tools + methodology

A skill is a single `.md` file that bundles a set of tools with the rationale / contract / cadence configuration for using them. The framework appends the skill body to the agent's system message (separated by `---`) — no per-prompt placeholder needed. The skill's tools are unioned into the agent's tool surface; its `reflect: {…}` / `extra_tools` blocks merge into the agent config. Skills are mounted at either the system level (`skills:` in `<agent>.system.md` frontmatter — every invocation of that agent gets the skill) or the user level (`skills:` in the per-call user message). Both layers union, deduped.

Current skills (`orchestra/skills/`):

| Skill | Bundles | Mounted on |
|---|---|---|
| `reflect` | the `reflect` tool + per-field contract + cadence default `interval: 5` | investigator |
| `prefer_delegation` | `agent_spawn` + `agent_list` + depth-as-upgrade rationale + `reflect.with_state: true` | reviewer |
| `diff_view` | `diff_list_files` + `diff_read_file` + `diff_outline` + `diff_search` + unified-diff methodology (ref forms, L/old/new coordinates, posting on `new`) | reviewer + investigator |
| `pr_threads` | `pr_list_threads` + `pr_read_thread` + `pr_read_comment` + look-only-when-relevant dedup rules | reviewer + investigator + dispatcher |
| `project_conventions` | pure-prose: the AGENTS.md / CONVENTIONS.md lookup pattern | reviewer + investigator |
| `finding_format` | pure-prose: finding-dict shape + severity rubric (BLOCKER/MAJOR/MINOR/COMMENT, calibrated against consequence) | reviewer + investigator |

Why split rather than inline: each skill is a single source of truth that BOTH reviewer and investigator (and dispatcher where applicable) consume. Updating the diff-view methodology happens in one file, not three.

### Cross-source surface (§10)

Tools that read PR / repo / discussion data accept an optional `repo=<uri>` parameter (and `pr=<id>` where applicable) to read from a repo OTHER than the current PR's. The URI standard is `bitbucket://<handle>/<project>/<repo>` (1-3 segments — server / project / leaf):

- `pr_get(repo, pr)`, `pr_list(repo)`, `repo_list(repo)` — three net-new tools for discovery + PR coordinates.
- `diff_*` and `pr_*_thread*` (the three read tools) get the `repo=`/`pr=` params.
- `jira_dev_info(ref)` is the bridge: returns the branches / commits / PRs Jira links to a ticket, with each PR pre-formatted as a ready `pr_get(repo=..., pr=...)` call.
- `jira_search_tickets(jql)` is the JQL discovery channel — find tickets beyond what the PR already links to.

In production these route through a real Bitbucket Registry (TBD); for tests they route through `FakeBitbucket.cross_source_*` payload maps. `"default"` resolves to the current PR's URI/id (existing behaviour, unchanged).

### Replay tier — record real PRs, replay against the current agent

Every webhook-triggered run can mirror its state to disk as a self-contained replay fixture: PR metadata + per-invocation snapshot + Jira responses + an incremental `git bundle` carrying every revision the PR went through. Once captured, a recording survives the upstream PR being merged, force-pushed, or deleted — the bundle is a real git repo with real refs and intact author identity, so `git checkout rev-NN` works exactly as it did on the day of capture.

Captured recordings show up under **`/qa/recordings`** in the QA UI. Each row is one PR; drill in to see its timeline of invocations and replay any of them through the bench.

#### Enabling capture

Two switches — one for one-off runs, one for the webhook router.

**One-off run** (`cli.py run`):

```bash
python cli.py run --pr-url ... --record-fixture ~/eden/diffgraph-recordings
# Or via env:
DIFFGRAPH_RECORD_DIR=~/eden/diffgraph-recordings python cli.py run --pr-url ...
```

**Webhook router** — per-agent opt-in in `webhook.toml`:

```toml
[agents.dg]
trigger = "cli"
command = '...'

[agents.dg.recording]
dir = "~/eden/diffgraph-recordings"
scope = "range"   # "range" (default — base..source + ancestry, ~10-50 MB / PR)
                  # "full"  (--all, ~100 MB-1 GB on big monorepos)
```

The router forwards `DIFFGRAPH_RECORD_DIR` + `DIFFGRAPH_RECORD_SCOPE` on the subprocess env, no template change to `command =` needed. Capture is best-effort: free space below 5 GB or an unwritable target disables capture for that run without aborting the agent. Pick a path on a partition with headroom — recordings can grow to tens of GB at scale (one bundle per PR, lots of PRs).

#### Replay modes

Two granularities, both reachable from the recording detail page or the bench CLI:

| Mode | Command | When to use |
|---|---|---|
| **Single invocation** | `bench replay-single <dir> --invocation N` | Drop today's agent into one captured PR-state moment, score against the LLM judge. Closest to existing unit-tier; deterministic given the recording. |
| **Full lifecycle** | `bench replay <dir>` | Walk the whole PR timeline. The agent acts at every recorded invocation point with **accumulating state** — humans replay verbatim from the recording, the agent's own outputs from earlier invocations of THIS replay carry forward as `[SELF]` context, exactly like a real PR proceeding. |

Lifecycle replay is the one that produces business-grade metrics. It implements the **orphan-skip rule** for free: a recorded human reply whose parent agent comment didn't get produced this run is silently dropped (cascade applies to chained replies) and counted as a divergence signal. Comment IDs are remapped at replay time — stable IDs in the recording (`a-NNN-K` for agent comments, `c-<bb-id>` for humans) map to dynamic runtime IDs as events apply.

#### Ground-truth labels and metrics

`outcomes.yaml` next to a recording carries human verdicts on every agent comment and a list of missed-finding nominations (TODO §19.9 schema). Auto-infer bootstraps it from captured human reactions:

```bash
# Auto-infer reads captured comments and applies marker-based rules:
#   "❌" / "[noise]" / "не согласен" / "false positive"  → noise
#   "+1" / "nice catch" / "согласен" / "поправлю"        → valid
#   thread resolved with no counter-reply                → valid (medium confidence)
#   conflicting signals                                  → undecided
bench outcomes-auto-infer ~/eden/diffgraph-recordings/.../PR-1234

# Or from the UI: "Auto-infer outcomes" button on the recording detail page.
```

Once `outcomes.yaml` exists, lifecycle replays auto-score against it:

| Metric | What it measures |
|---|---|
| `miss_rate` / `miss_rate_blocker` | Issues humans raised that the agent never surfaced. The `_blocker` variant filters to BLOCKER/MAJOR — the only miss rate that matters to the business. |
| `cumulative_noise_rate` | Agent comments humans labelled noise / total agent comments. The cost the agent imposes on reviewers per finding produced. |
| `convergence_invocations[topic]` | At which invocation index a verified finding first appeared. "Found in rev-01" beats "found after rev-03 / force-push". |
| `drift_alerts` | A topic raised in invocation N but absent from N+1 — agent forgot a true finding. |
| `orphan_skip_count` | How many recorded human replies got dropped because today's agent took a different topic — a divergence signal. |

#### What this unlocks

Once a corpus accumulates (50-100 labelled recordings is enough), the standard moves become real regression measurements:

- **Prompt change**: every commit on master runs the replay corpus → score deltas surface before merge.
- **Model migration**: replay corpus tells you whether `miss_rate_blocker` regressed before flipping the production endpoint.
- **Skill A/B**: replay corpus measures BUSINESS impact (miss/noise/stability), not just structural extraction.
- **Production drift detection**: same recording, weekly replay → catch silent regressions when nothing in your code changed but model endpoint / prompt cache / provider did.

See `TODO.md §19` for the full design and `tests/test_recording.py` for end-to-end coverage of capture + replay + orphan-skip + scoring.

### Data the agent sends to the LLM

Conceptually, what leaves the agent process and reaches the LLM provider falls into three layers, each with a distinct source-of-truth and lifecycle:

| Layer | Source | Lifecycle |
|---|---|---|
| **Prompt scaffold** — methodology, tool contracts, skill bodies | Static files in `diffgraph/prompts/` + `orchestra/skills/` | Versioned with the codebase; identical across runs of the same revision |
| **Task context** — PR metadata, diff hunks, file contents, PR threads, Jira ticket bodies, dev-info linkage | Pulled at runtime through tool calls (`diff_*`, `pr_*`, `jira_*`, `read_file`, `search`, `read_outline`) | Per-run snapshot; the slice fetched during one review |
| **Reasoning history** — tool-call arguments, tool results, model's intermediate text | Accumulated by the agent loop within one run | Discarded at end-of-run; persisted only to the local trace store |

Three properties make the flow auditable end-to-end:

1. **Visibility is bounded by the bot account's permissions.** Every external read uses the configured Bitbucket bot user's token and the Jira service-account token. Repos, projects, branches, and PRs the bot is not entitled to see remain invisible to the tools, and therefore can never reach the prompt. The agent inherits its principal's authorization surface; it does not bypass it. Cross-source reads (`repo=`, `pr=`) follow the same rule — the other-repo content is fetched through the same token and is only available where that token already had access.
2. **The tool layer is the only egress path.** No arbitrary filesystem access outside the lazily-cloned working copy of the current PR, no arbitrary HTTP, no shell. The complete set of channels the agent can read from is the registered tool list in `diffgraph/orchestra_tools.py`; the complete set of channels it can write to is the same list (`pr_post_comment`, `set_review_status`). Every call is recorded — name, arguments, result — in `~/.diffgraph/traces.db` and, optionally, mirrored on disk under `--trace-dir`.
3. **The provider boundary is one explicit endpoint.** The LLM endpoint is selected per run via `--provider`, resolving to a single profile in `.llm_creds.toml`. Switching between a public API, a self-hosted vLLM, or an on-prem endpoint is a configuration change, not a code change — the agent's data flow is identical, only the destination URL moves.

The model a security review usually cares about: the agent is a constrained client of three things — a versioned prompt set, the bot account's read surface, and one LLM endpoint — and every artefact crossing any of those boundaries is enumerable and traced.

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

**Agent isolation for testing.** `--mocks <fixture.yaml>` short-circuits any `agent_spawn` / `read_file` / etc. tool call with a canned response (Mockito-style). `--user-message-from <file>` overrides the agent's default user template, so the same reviewer can be tested against different task framings (concerns-only, consolidation-only, full pipeline) without changing the system prompt. `--invocations-out <path>` captures every tool call to a JSON file for the test judge to verify against. See `orchestra/tool_mocks.py` for the fixture format.

### Prompt architecture — layered, extension-friendly

Every agent ships as **two sibling files** under `diffgraph/prompts/`:

| File | Layer | What lives here |
|---|---|---|
| `<agent>.system.md` | **system / methodology** | What the agent *is*, independent of the invocation surface: base `tools:`, `summary:`, `budget:`, `reflect_interval:`, `llm:`, methodology guards (e.g. `text_response`), and the methodology body (severity rubric, finding shape, diff-view contract). |
| `<agent>.user.md` | **user / interface** | How the agent is *invoked* in a concrete environment: `tools_add:` (interface tools — `pr_post_comment`, `set_review_status`, …), `data:` (spawn-time args from the surface), interface guards (e.g. `require_tool:pr_post_comment`), `extra_tools:`, `dispatch_mode`, and the per-call task wording with `{placeholder}` interpolation. |

The system layer is the **closed-for-modification base class** (methodology); the user layer is the **open-for-extension** interface wrapper. The same `reviewer.system.md` is shared by production Bitbucket, every isolated test prompt, and every consolidation/concerns-text fixture — only the user layer changes per call.

The compiler **merges** `data:` and `guards:` from both layers into one `AgentRegistryEntry`. A key declared in both layers is a hard error so a rename can't silently shadow the other side.

```yaml
# diffgraph/prompts/reviewer.system.md  (stable methodology + base toolkit)
---
agent: reviewer
mode: react
summary: >
  Code review lead. Analyzes a PR diff, identifies concerns scaled
  to diff size, spawns focused investigators, consolidates findings.

# Minimum surface every reviewer task needs. Per-task extensions
# (publishing, delegation, verdict) opt in via the user layer.
tools:
  - diff_read_file
  - diff_outline
  - diff_list_files
  - diff_search
  - reflect
  - done

# `data:` is interface-specific and lives in user.md — methodology
# here is independent of the invocation surface.

budget:
  tokens: 50000
  steps: 50
reflect_interval: 5

llm:
  temperature: 0.2
---
Code review lead. Execute the task described in the user message.
Diff view, severity rubric, finding shape, … (the *how*).
```

```yaml
# diffgraph/prompts/reviewer.user.md  (production task layer)
---
# Extension points — additive only. `tools:` (full-replace) is
# rejected by the compiler at this layer to stop a per-task prompt
# from silently overriding the agent's base contract.
tools_add:
  - pr_list_threads
  - pr_read_thread
  - agent_list
  - agent_spawn
  - pr_post_comment
  - set_review_status

# Interface contract — data the reviewer receives at spawn time
# under the Bitbucket-PR-comment surface.
data:
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"
  commits:
    type: string
    from: pr_context.commits
---
PR: {pr_title}
{pr_description}

Review this PR end-to-end. Spawn investigators for any concern that
needs depth; consolidate; publish via pr_post_comment; verdict via
set_review_status; finish with done(findings).
```

```yaml
# diffgraph/test_prompts/reviewer/concerns-text.md  (test task layer)
---
# Replace the publishing extension with a capture-style text channel.
# Works on tool_choice=required providers (DeepSeek) that can't emit
# a tool-less text turn. The judge reads `text_answer.text` back
# via assert_via=[intended_text].
tools_add:
  - text_answer
extra_tools:
  - name: text_answer
    description: "Submit your final concerns list. Plain text, one per line."
    parameters:
      type: object
      properties:
        text:
          type: string
      required:
        - text
---
PR: {pr_title}
{pr_description}

Identify the concerns this diff raises and submit them via
text_answer(text=...). Then call done(findings=[]).
```

#### Why two layers, not one

- **Isolation**: a unit test swaps the user layer (`--user-message-from <file>`) and runs the same reviewer system prompt against a different task framing — concerns-text, consolidation-only, full pipeline — without touching production methodology. The `concerns-only` regression-test fixture, the `consolidation-buy3get1` consolidation fixture, and the production review all share **one** system prompt.
- **Testability**: extension points are explicit (`tools_add`, `extra_tools`, `dispatch_mode`), so each test prompt declares the exact surface it needs and the compiler validates it. No hidden inheritance, no monkey-patching.
- **Compose, don't override**: `tools_add` is additive — the base toolkit is always present. A user-layer `tools:` (full-replace) raises at compile time: it would silently mask the agent's base contract, which is exactly the class of bug the layered model exists to prevent.
- **Capture tools as a per-task channel**: `extra_tools` declares one-off tools (e.g. `text_answer`, `submit_answer`) registered into the registry just for this run. Their handler echoes args, so the judge can read the agent's intended deliverable back from `invocations.json` without a side-channel.

The same model extends to dispatcher and investigator: `<agent>.system.md` defines the methodology contract; every concrete task — production or test — extends it through a `<agent>.user.md` (or `test_prompts/<agent>/<case>.md`) with frontmatter-declared extensions.

### Key features

| Feature | Description |
|---|---|
| `data:` + `from:tool.field` | Auto-resolve prompt data from cached tool calls |
| `guards:` | Reactive guards: `text_response`, `require_tool:X` |
| `tools: [agent_spawn, …]` | Agent can spawn children — `agent_spawn` lives in the same flat tool list as everything else |
| JSON Schema validation | All tool calls validated before dispatch (jsonschema) |
| Trace system | SQLite WAL, live WebSocket view, navigator with per-step detail |
| SGR | Self-Guided Reasoning with question IDs and fuzzy matching |
| Pusher pipeline | Producers (ratio / time-budget / reflect cadence) → apply → trace middleware chain (`orchestra/budget.py`) |
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
+-- prompts/                   Two-file layout per agent: system + user
    +-- dispatcher.system.md   Frontmatter (tools, budget) + routing/thread methodology
    +-- dispatcher.user.md     Per-call template ({comment_thread}, {message}, …)
    +-- reviewer.system.md     Frontmatter + severity rubric + finding shape + AGENTS.md rule
    +-- reviewer.user.md       Production task: tools_add publishing+delegation, end-to-end review
    +-- investigator.system.md Frontmatter + reflect rules + investigation methodology
    +-- investigator.user.md   Default task: investigate one focused concern
+-- test_prompts/              Sibling test-only user layers (same system base)
    +-- reviewer/
        +-- concerns-text.md       tools_add text_answer — concerns-as-text deliverable
        +-- consolidation-*.md     tools_add publishing — hardcoded findings → publish

diffsearch/                  Virtual unified diff filesystem
webhook/                     Bitbucket webhook router with A/B routing
tracing/                     Trace web server (FastAPI + Alpine.js)
quality_api/                 QA orchestration — task queue, worker pools, discovery
quality_cli/                 QA worker / search / tasks CLI
benchmarks/                  Code-review agent benchmark — scenarios + judge
+-- cli.py                   run (integration) / run-unit / report / history
+-- runner/                  scenario loader, LLM judge, scorer, run / run_unit
+-- scenarios/               unit/ (isolation) + java/ + interaction/ (full pipeline)
+-- fixtures/                user-message overrides + ToolMocks fixtures
evolution/                   Self-sustaining prompt development
docker/                      Dockerfile + entrypoint
```

`benchmarks/` was a separate repo (`code-review-benchmarks`) until the
May-2026 monorepo merge — it's now a `git subtree` here. One checkout,
one `.venv`, one `.env`; the QA server runs the bench from inside its
own tree. See [`benchmarks/README.md`](benchmarks/README.md) for how to
run scenarios and [`docs/qa-architecture.md`](docs/qa-architecture.md)
for the full quality loop.

## Running as systemd services on RHEL

Two daemons ship with DiffGraph:

- **Webhook router** — `python -m webhook --config webhook.toml` (default port `8000`)
- **QA server** (`diffgraph-qa.service`, was `diffgraph-trace`) — the FastAPI app in `tracing/server`, default port `8080`. One process serving three things: the per-run **trace viewer** (`/`, `/runs/{id}`), the **Quality API + UI** (`/qa/*`, `/api/qa/*`) for scheduled cross-mutation evaluation — configurable schedules with tag-filtered scenarios, plan/queue, per-mutation scoring (hard skill / soft skill / methodology), on-demand fire, plan cancel — and the **WorkerSupervisor** that spawns bench-task workers on demand. The trace viewer is just a subset of what it serves, hence the name.

Systemd unit templates + install/reload helpers live under `scripts/`.

### Prerequisites

Clone the repo, create the venv, install deps, and fill in configs as usual:

```bash
cd /opt/diffgraph                            # or wherever you checked out the repo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # includes the benchmarks/ subtree deps
cp .env.example .env                         # edit: API keys, tokens, CA bundle
cp .llm_creds.toml.example .llm_creds.toml   # edit: LLM provider profiles
cp webhook/config.example.toml webhook.toml  # edit: routes, agents
cp config.yaml config.local.yaml             # edit: LLM api_url / model
cp benchmarks/config.yaml benchmarks/config.local.yaml  # edit: bench Bitbucket + judge
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
2. Writes `diffgraph-webhook.service` and `diffgraph-qa.service` to `/etc/systemd/system/`.
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
sudo systemctl status  diffgraph-webhook diffgraph-qa
sudo systemctl restart diffgraph-webhook diffgraph-qa
sudo systemctl stop    diffgraph-webhook diffgraph-qa
sudo systemctl disable diffgraph-webhook diffgraph-qa   # stop auto-start on boot
```

### Logs

Both services log to the systemd journal under their `SyslogIdentifier`:

```bash
# Follow live
journalctl -u diffgraph-webhook -f
journalctl -u diffgraph-qa -f

# Last N lines
journalctl -u diffgraph-webhook -n 200 --no-pager

# Since a time window
journalctl -u diffgraph-webhook --since "1 hour ago"
journalctl -u diffgraph-webhook --since today

# Errors only
journalctl -u diffgraph-webhook -p err

# Both at once, live
journalctl -u diffgraph-webhook -u diffgraph-qa -f
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

## Kubernetes — pod sizing & deployment notes

No GPU needed — all LLM inference is external (OpenAI / DeepSeek / Cloud.ru / etc). Locally the pod runs the FastAPI webhook router (`:8000`), the QA / trace server (`:8080`), and spawns per-PR `cli.py run` subprocesses on demand.

### Pod resources (recommendations)

| Tier | Use case | requests | limits | Storage |
|---|---|---|---|---|
| **Minimum** | dev / staging, 1 PR at a time | `cpu: 200m, memory: 768Mi` | `cpu: 1000m, memory: 1500Mi` | 5–10 Gi |
| **Standard** | production, ~5–10 concurrent agents | `cpu: 500m, memory: 2Gi` | `cpu: 2000m, memory: 4Gi` | 20–50 Gi |
| **Heavy / bench** | parallel scenarios, QA + production combined | `cpu: 1000m, memory: 4Gi` | `cpu: 4000m, memory: 8Gi` | 50–100 Gi |

Memory split: FastAPI servers idle at ~250-400 MB RSS (both processes combined); tree-sitter parsers add ~20-40 MB once warmed (17 languages, lazy-loaded — see [`diffgraph/lang.py`](diffgraph/lang.py)); each per-PR agent subprocess uses ~120-200 MB RSS while running.

### Storage

- **`/data` — persistent volume (required)**. Houses `~/.diffgraph/traces.db` (SQLite trace DB + WAL), QA plans, scoring history. Don't use `emptyDir` — losing it nukes all run history and QA state.
- **`/tmp/diffgraph-*` — ephemeral clones**. Per-PR git clone for the diff view, cleaned after each run. `emptyDir { sizeLimit: 5Gi }` is fine for small repos; bump to 20+ Gi for codebases > 500 MB.
- **`/app/.env` + `/app/config.local.yaml`** — Secret + ConfigMap mounts. Same files you'd `source` locally.
- **`/app/certs/`** — corporate CA bundles + client certs. Mount from a Secret.

### Networking

**Egress** must reach: LLM API endpoint (OpenAI / DeepSeek / Cloud.ru / etc), Bitbucket Server (PR/diff API + webhook callbacks), Jira (if integration enabled).

**Ingress**:
- `:8000` (webhook router) — public URL for Bitbucket webhooks (`/webhooks/bitbucket`).
- `:8080` (QA UI + trace viewer) — internal-only for developers.

### Replicas

**1 replica.** SQLite is a single-writer DB; the trace + QA state is owned by this pod. Higher load → scale UP (more CPU/RAM) rather than out. If you genuinely need horizontal scaling, split the QA server into a separate Deployment with a shared PVC and put a queue between webhook and workers (`quality_api/` already supports the worker-pool shape) — but the single-pod default is enough for hundreds of PRs/day on the Standard tier.

### Probes

```yaml
readinessProbe:
  httpGet: { path: /, port: 8080 }
  periodSeconds: 10
livenessProbe:
  httpGet: { path: /, port: 8080 }
  periodSeconds: 30
  failureThreshold: 3
```

Liveness on `:8080` because that's the always-on QA server; `:8000` only fires on webhook events. If `:8080` stops responding, restart.

### What dominates pod resource use

- **Active agent subprocesses** — each adds ~150 MB RSS for the duration of one PR review (5-30s typically; up to a few minutes on big diffs or slow LLM endpoints). Plan capacity around peak concurrent agents, not average.
- **SQLite trace WAL** — grows with usage; checkpoints run periodically. `/data` PV needs the headroom listed above.
- **Lazy git clones** — `/tmp/diffgraph-*` holds one per-PR clone at a time per active agent. Auto-cleaned. Big monorepos can spike `/tmp` use.
- **LLM call latency** — the pod is mostly waiting on network I/O during agent runs. CPU spikes are short (tree-sitter parsing + git diff). Network bandwidth + provider latency matters more than CPU.

---

## Tests

```bash
source .venv/bin/activate
pytest
```
