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
  "pr:comment:added" = "parse"     ← extract /command from comment
  │
  ▼
Layer 2: Routes (first match wins, cascade via sample)
  ┌─ forward = "pra"  → event-level: forward raw event, done
  └─ agent = "dg2"    → command-level: route each command
       ├─ review → dg2
       └─ improve → pra  (per-command override)
  │
  ▼
Layer 3: Agent trigger
  cli     → subprocess with {pr_url}, {args}, {comment_id}
  http    → POST to agent API
  webhook → forward raw Bitbucket event
```

## Two routing modes

Each route is **either `forward` or `agent`**, never both:

| Key | Mode | Behavior |
|-----|------|----------|
| `forward = "pra"` | Event-level | Forward raw Bitbucket event to agent. No command extraction. Agent handles everything internally. |
| `agent = "dg2"` | Command-level | Extract commands from `[events]` config, route each command to agent. Per-command overrides possible. |

## Cascading with `sample`

`sample` controls what percentage of PRs a route matches (deterministic by `hash(pr_url)`). Unmatched PRs fall through to the next route.

```toml
# 50% of PLATFORM PRs → forward to pr-agent
[[routes]]
name = "platform-forward"
when = "project == 'PLATFORM'"
forward = "pra"
sample = 50

# Remaining 50% → command routing to dg2
[[routes]]
name = "platform-commands"
when = "project == 'PLATFORM'"
agent = "dg2"
```

Same PR always gets the same route (hash is deterministic). Three-way split:

```toml
[[routes]]
name = "v3-canary"
when = "project == 'X'"
agent = "dg3"
sample = 10                    # 10%

[[routes]]
name = "v2-rollout"
when = "project == 'X'"
agent = "dg2"
sample = 50                    # 50% of remaining 90% ≈ 45%

[[routes]]
name = "v1-stable"
when = "project == 'X'"
agent = "dg1"                  # rest ≈ 45%
```

## Config

```toml
[server]
port = 8000

[agents.dg2]
trigger = "cli"
command = '... cli.py run --pr-url="{pr_url}" --post-comments'
timeout = 600

[agents.dg2.commands]
ask = '... cli.py ask --pr-url="{pr_url}" --question="{args}"'
improve = '... cli.py improve --pr-url="{pr_url}" --comment-id={comment_id}'

[agents.pra]
trigger = "webhook"
base_url = "http://pr-agent-host:3000"

[events]
"pr:opened" = ["review"]
"pr:comment:added" = "parse"
"repo:refs_changed" = []

[[routes]]
name = "legacy-forward"
when = "project == 'LEGACY'"
forward = "pra"

[[routes]]
name = "canary"
when = "repo == 'my-service'"
agent = "dg2"

[[routes]]
name = "sbloom"
when = "project == 'SBLOOM'"
agent = "dg2"
improve = "pra"

[[routes]]
name = "default"
when = "true"
agent = "dg1"
```

## Commands from PR comments

```
@diffgraph /review
@diffgraph /ask What about null safety?
@diffgraph /improve
```

| Part | Extracted as |
|---|---|
| `/review` | command = `review` |
| `/ask What about null?` | command = `ask`, args = `What about null?` |
| `/improve` (as reply in thread) | command = `improve`, comment_id = invoking comment ID |

`@mention` is optional. `comment_id` is the ID of the comment containing the /command — agent can reply directly to it.

## Placeholders

Command templates (`command`, `commands.<name>`) support:

| Placeholder | Value |
|---|---|
| `{pr_url}` | Full PR URL |
| `{pr_id}` | PR number |
| `{project}` | Bitbucket project key |
| `{repo}` | Repository slug |
| `{command}` | Command name |
| `{args}` | Text after /command |
| `{comment_id}` | Invoking comment ID |

## Route matching

**`when`** — Python expression against PR metadata: `project`, `repo`, `author`, `branch`, `target`, `pr_url`, `pr_id`, `title`.

**`sample`** — percentage (0-100). Only this % of PRs (by hash) match. Default 100. Unmatched fall through.

**Per-command override** — any key besides `name`, `when`, `agent`, `forward`, `sample` is a command override:
```toml
agent = "dg2"          # default
improve = "pra"         # /improve → pra instead
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

Route management API enables evolution to deploy/undeploy/rebalance branches programmatically.

## Tests

```bash
pytest webhook/tests/ -v --log-cli-level=INFO
```

41 tests: config, event parsing, routing, API CRUD (create/update/delete routes).
