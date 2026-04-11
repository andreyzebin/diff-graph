# Webhook Router

Bitbucket Server webhook receiver with A/B agent routing. Routes PR events to different agent versions based on TOML config rules.

## Quick start

```bash
cp webhook/config.example.toml webhook.toml
# edit webhook.toml — set agents, routes
python -m webhook --config webhook.toml
```

Configure Bitbucket webhook URL: `http://your-host:8000/webhook`

## Config

```toml
[agents.dg2]
trigger = "cli"
command = 'cd ~/repos/diff-graph && source .env && .venv/bin/python cli.py run --pr-url="{pr_url}" --post-comments'
timeout = 600

[agents.dg1]
trigger = "cli"
command = 'cd ~/repos/diff-graph-v1 && .venv/bin/python cli.py run --pr-url="{pr_url}" --post-comments'

[events]
"pr:opened" = ["review"]           # auto-run on PR creation
"pr:comment:added" = "parse"       # extract /command from comment
"repo:refs_changed" = []            # nothing on push

[[routes]]
name = "canary"
when = "repo == 'my-service'"
agent = "dg2"                       # all commands → dg2

[[routes]]
name = "ab-test"
when = "project == 'MYPROJECT'"
agent = { dg2 = 30, dg1 = 70 }    # 30/70 A/B split
improve = "pra"                     # except /improve → pra

[[routes]]
name = "default"
when = "true"
agent = "dg1"
```

## Routing

Routes evaluated top to bottom, first match wins.

**`when`** — Python expression evaluated against PR metadata:
- `project` — Bitbucket project key (e.g. "SBLOOM")
- `repo` — repository slug
- `author` — PR author username
- `branch` — source branch
- `target` — target branch
- `pr_url`, `pr_id`, `title`

**`agent`** — default for all commands:
- `"dg2"` — 100% to this agent
- `{ dg2 = 30, dg1 = 70 }` — A/B split, deterministic by `hash(pr_url)`. Same PR always gets same agent.

**Per-command override** — any key besides `name`, `when`, `agent`:
```toml
agent = "dg2"          # default
improve = "pra"         # /improve goes to pra instead
```

## Events

| Bitbucket event | Config | Behavior |
|---|---|---|
| `pr:opened` | `["review", "describe"]` | Auto-run listed commands |
| `pr:comment:added` | `"parse"` | Extract `/command` from comment text |
| `repo:refs_changed` | `[]` | Ignore |

## Agents

| Field | Description |
|---|---|
| `trigger` | `"cli"` (subprocess) or `"http"` (POST to API) |
| `command` | Shell command with `{pr_url}`, `{pr_id}`, `{project}`, `{repo}` placeholders |
| `base_url` | For http trigger |
| `timeout` | Seconds (default 600) |

## Endpoints

- `POST /webhook` — Bitbucket webhook receiver
- `GET /health` — health check with agent/route counts
- `GET /routes` — show configured routes (debugging)

## Tests

```bash
pytest webhook/tests/ -v --log-cli-level=INFO
```

30 tests covering config loading, event parsing, command extraction, route matching, A/B distribution, per-command overrides.
