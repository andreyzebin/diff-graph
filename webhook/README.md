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

# Per-command templates (override default for specific commands)
[agents.dg2.commands]
ask = '... cli.py ask --pr-url="{pr_url}" --question="{args}"'
improve = '... cli.py improve --pr-url="{pr_url}" --comment-id={comment_id}'

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

## Commands from PR comments

Users invoke commands by commenting on a PR:

```
@diffgraph /review
@diffgraph /ask What about null safety in this method?
@diffgraph /improve
@diffgraph /help How do I fix this?
```

The `@mention` part is optional — `/review` alone works too. The router extracts:

| Part | Extracted as |
|---|---|
| `/review` | command name = `review` |
| `/ask What about null safety?` | command = `ask`, args = `What about null safety?` |
| `/improve` in a thread reply | command = `improve`, comment_id = parent comment ID |

### Threaded commands

When `/improve` or `/ask` is posted as a reply to an existing comment thread, the router captures the **parent comment ID**. This lets the agent know which specific code comment to address.

```
Thread:
  [reviewer] "This null check is inconsistent"     ← comment #150
    [user] "@diffgraph /improve"                    ← reply, parent=#150
```

The router sends `command=improve, comment_id=150` to the agent.

## Placeholders

Command templates support these placeholders:

| Placeholder | Value | Available |
|---|---|---|
| `{pr_url}` | Full PR URL | Always |
| `{pr_id}` | PR number | Always |
| `{project}` | Bitbucket project key | Always |
| `{repo}` | Repository slug | Always |
| `{command}` | Command name (review, ask, ...) | Always |
| `{args}` | Text after command (/ask **question**, /help **topic**) | When present |
| `{comment_id}` | Parent comment ID (threaded replies) | When reply in thread |

### Per-command templates

Different commands may need different CLI invocations. Use `[agents.<name>.commands]` to override the default `command` for specific commands:

```toml
[agents.dg2]
trigger = "cli"
command = '... cli.py run --pr-url="{pr_url}" --post-comments'

[agents.dg2.commands]
ask = '... cli.py ask --pr-url="{pr_url}" --question="{args}"'
improve = '... cli.py improve --pr-url="{pr_url}" --comment-id={comment_id}'
help = '... cli.py help --pr-url="{pr_url}" --topic="{args}"'
```

If no per-command template exists, the default `command` is used.

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
- `{ dg2 = 30, dg1 = 70 }` — A/B split, deterministic by `hash(pr_url)`. Same PR always gets same agent across all events.

**Per-command override** — any key besides `name`, `when`, `agent`:
```toml
agent = "dg2"          # default for all commands
improve = "pra"         # /improve goes to pra instead
```

## Events

| Bitbucket event | Config | Behavior |
|---|---|---|
| `pr:opened` | `["review", "describe"]` | Auto-run listed commands |
| `pr:comment:added` | `"parse"` | Extract `/command` from comment text (with optional `@mention`) |
| `repo:refs_changed` | `["review"]` or `[]` | Auto-run on push, or ignore |

## Agents

| Field | Description |
|---|---|
| `trigger` | `"cli"` (subprocess) or `"http"` (POST to API) |
| `command` | Default shell command template with `{placeholder}` substitution |
| `commands.<name>` | Per-command template overrides |
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

34 tests covering config loading, event parsing, @mention extraction, command args, threaded comment_id, route matching, A/B distribution, per-command overrides, args/comment_id preservation through routing.
