# Webhook Router

Bitbucket Server webhook receiver with two-level routing: event-level (forward) and command-level (per-command agent selection). Supports A/B testing via `sample` percentage.

## Quick start

```bash
cp webhook/config.example.toml webhook.toml
# edit webhook.toml
python -m webhook --config webhook.toml
```

## Architecture

```
Bitbucket event
  │
  ▼
Layer 1: Event → Commands
  "pr:opened" = ["review"]         ← auto-run commands
  "pr:comment:added" = "parse"     ← extract /command or plain text
  │
  ▼
Layer 2: Routes (first match wins, cascade via sample)
  ┌─ forward = "pra"   → forward raw event to external agent
  └─ agent = "dg"      → route to Revvy CLI
       ├─ /review  → dispatcher → spawns reviewer
       ├─ /help    → dispatcher → replies directly
       └─ default  → dispatcher → answers from context
  │
  ▼
Layer 3: Agent trigger
  cli     → subprocess: cli.py run --pr-url --message --comment-id
  http    → POST to agent API
  webhook → forward raw Bitbucket event
```

## Config

```toml
[server]
port = 8000

# Revvy — dispatcher handles all interactions
[agents.dg]
trigger = "cli"
command = '... cli.py run --pr-url="{pr_url}" --message="{message}" --comment-id={comment_id}'
timeout = 600

# Revvy — direct reviewer (skip dispatcher)
[agents.dg-review]
trigger = "cli"
command = '... cli.py run --pr-url="{pr_url}" --agent=reviewer'
timeout = 600

# Useful cli.py flags worth wiring through the command:
#   --provider <name>   pick an LLM profile from ~/repos/.llm_creds.toml
#                       (deepseek, qwen3, qwen3-6, ...)
#   --bot-user <slug>   tag the bot's own existing comments as [SELF]
#                       (or export BOT_USER=<slug> in .env)
#   --trace-dir <path>  mirror per-step LLM/tool traces to disk
#   --prompts <uri>     pin prompts (path, file://, bitbucket://...). The
#                       directory name → generation; content hash → mutation
#                       (visible to the dispatcher prompt and trace DB).

# PR-Agent — forward raw event
[agents.pra]
trigger = "webhook"
base_url = "http://pr-agent-host:3000/webhook"

[events]
"pr:opened" = ["review"]
"pr:comment:added" = "parse"
"pr:from_ref_updated" = ["review"]
"repo:refs_changed" = []

[[routes]]
name = "default"
when = "true"
agent = "dg"
review = "dg-review"        # auto-review and /review → direct reviewer
```

## Health checks

Rented-GPU vLLM endpoints (cloud.ru, etc.) suspend after ~30 min idle
and take 10–15 min to cold-start. The router can keep one or more
endpoints warm by firing a tiny `cli.py health` ping on a schedule.

```toml
[[health]]
name             = "qwen3-6"
command          = "cd ~/repos/diff-graph && source .env && .venv/bin/python cli.py health --provider qwen3-6 -q"
interval_seconds = 180          # every 3 min
timeout_seconds  = 1200         # tolerate cold start (20 min)
time_window      = "09:00-19:00" # MSK working hours; empty = always
timezone         = "Europe/Moscow"
days             = [1, 2, 3, 4, 5]  # Mon-Fri; empty/omitted = every day
```

Multiple `[[health]]` blocks run in parallel — one per endpoint.

| Field | Default | Meaning |
|-------|---------|---------|
| `command` | _(required)_ | Shell command. Exit 0 = healthy, anything else = WARNING in logs. |
| `interval_seconds` | `180` | How often to fire the check. |
| `timeout_seconds` | `1200` | Per-call timeout. Cold starts can take 10–15 min, so default is generous. |
| `time_window` | `""` _(always)_ | `HH:MM-HH:MM` in `timezone`. Window across midnight is supported (e.g. `22:00-06:00`). |
| `timezone` | `Europe/Moscow` | IANA tz name. |
| `days` | `[]` _(all)_ | ISO weekdays — `1=Mon..7=Sun`. |

Each tick spawns the command in the background, so a 15-minute
cold-start does not block the next interval. Failures (non-zero exit,
timeout, launch error) are logged at `WARNING` — there's no retry,
the next tick will try again.

## Two routing modes

Each route is **either `forward` or `agent`**, never both:

| Key | Mode | Behavior |
|-----|------|----------|
| `forward = "pra"` | Event-level | Forward raw Bitbucket event to agent. No command extraction. |
| `agent = "dg"` | Command-level | Extract commands from `[events]`, route each to agent. Per-command overrides possible. |

## Cascading with `sample`

`sample` controls what percentage of PRs a route matches (deterministic by `hash(pr_url)`). Unmatched PRs fall through to the next route.

```toml
# A/B test: 50% pr-agent, 50% Revvy
[[routes]]
name = "platform-pra"
when = "project == 'PLATFORM'"
forward = "pra"
sample = 50

[[routes]]
name = "platform-dg"
when = "project == 'PLATFORM'"
agent = "dg"
```

## Commands from PR comments

Any PR comment triggers the dispatcher agent. The dispatcher handles:

```
/review                          → spawns reviewer agent
/help                            → replies with version + commands
/help what does /review do?      → answers the question
/ask how does this work?         → answers from PR context (planned cmd, helpful fallback)
Is this null-safe?               → answers from PR context
```

`@mention` prefix is optional. Comments without `/command` are passed as-is (command = `default`).

When a command is posted as a reply in a thread, the webhook captures `comment_id` — the dispatcher can reply directly in that thread.

### Adding a new command

The webhook is command-agnostic — it routes strings, not predefined enums. To add a new command:

1. Add it to `[events]` if it should auto-trigger (e.g. `"pr:opened" = ["review", "describe"]`)
2. The dispatcher agent handles it — update `diffgraph/prompts/dispatcher.system.md` (capabilities) or `dispatcher.user.md` (per-call task framing) to recognize the new command
3. Per-command routing works automatically via route config overrides

## Placeholders

Command templates support:

| Placeholder | Value |
|---|---|
| `{pr_url}` | Full PR URL |
| `{pr_id}` | PR number |
| `{project}` | Bitbucket project key |
| `{repo}` | Repository slug |
| `{command}` | Command name (`review`, `help`, `default`, ...) |
| `{args}` | Text after /command (or full text for `default`) |
| `{message}` | Full user message: `/review args` for commands, raw text for `default` |
| `{comment_id}` | Invoking comment ID |

## Route matching

**`when`** — Python expression against PR metadata: `project`, `repo`, `author`, `branch`, `target`, `pr_url`, `pr_id`, `title`.

**`sample`** — percentage (0-100). Deterministic by hash. Default 100.

**Per-command override** — any key besides `name`, `when`, `agent`, `forward`, `sample` is a command override:
```toml
agent = "dg"             # default: all commands → dispatcher
review = "dg-review"     # /review → direct reviewer (skip dispatcher)
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook` | Bitbucket webhook receiver |
| `GET` | `/health` | Health check |
| `GET` | `/routes` | Show all routes |
| `POST` | `/api/routes` | Create route `{name, when, agent, sample}` |
| `PATCH` | `/api/routes/{name}` | Update route `{sample?, agent?, when?}` |
| `DELETE` | `/api/routes/{name}` | Delete route |

## Tests

```bash
pytest webhook/tests/ -v --log-cli-level=INFO
```
