# Commands

Commands are how users and automation interact with the code review agent. A command is a named action that can be triggered automatically (on PR events) or manually (via PR comments).

## Command types

### Auto commands

Triggered by Bitbucket events without user action. Configured in `[events]`:

```toml
[events]
"pr:opened" = ["review"]           # auto-review on PR creation
"pr:from_ref_updated" = ["review"] # re-review on force-push
```

The agent runs the command immediately when the event fires. No comment needed.

### Slash commands

Triggered by a PR comment. The webhook parses `/command` from the comment text:

```
/review
/ask What about null safety here?
/improve
/describe
```

Configured as:

```toml
[events]
"pr:comment:added" = "parse"    # extract /command from comment
```

`@mention` prefix is optional (`@diffgraph /review` and `/review` both work).

### Commands with arguments

Some commands accept free-text after the command name:

```
/ask What happens if the input is empty?
/ask_line Explain this regex
```

The text after `/command` is passed as `{args}` to the agent template.

### Commands with context

When a slash command is posted as a reply in a PR comment thread, the webhook captures `comment_id` of the invoking comment. The agent can use this to reply directly in the same thread.

```
/improve     ← posted as reply to a specific comment
              → agent gets comment_id, can reply in context
```

## Command catalog

Commands the webhook can route. Which are available depends on agent capabilities.

| Command | Type | Description |
|---------|------|-------------|
| `/review` | auto, manual | Full code review — analyze diff, investigate concerns, post findings |
| `/describe` | auto, manual | Generate PR title, summary, and labels from the diff |
| `/improve` | manual | Suggest code improvements for specific files or the whole PR |
| `/ask <question>` | manual | Answer a free-text question about the PR changes |
| `/ask_line <question>` | manual | Answer a question about a specific code line (posted as line comment reply) |
| `/update_changelog` | auto, manual | Update changelog based on PR contents |
| `/add_docs` | manual | Generate docstrings for new functions/classes in the PR |
| `/generate_labels` | manual | Create labels based on PR content analysis |
| `/help` | manual | Show available commands and current configuration |
| `/config` | manual | Display or update agent configuration |

Not every agent supports every command. The webhook routes commands to agents — if an agent doesn't support a command, it's the agent's responsibility to respond with a helpful message.

## Routing commands to agents

Each command is independently routable:

```toml
[[routes]]
name = "mixed"
agent = "dg2"           # default: all commands → dg2
improve = "pra"          # override: /improve → pr-agent instead
```

This enables mixing agents per capability — one agent for review, another for code suggestions.

## Adding a new command

1. Add the command to `[events]` if it should auto-trigger on an event
2. The webhook extracts it and routes it — no code changes needed
3. The agent receives `{command}`, `{args}`, `{comment_id}` via its template
4. Per-command agent overrides work automatically via route config

The webhook is command-agnostic — it routes strings. New commands are a config change, not a code change.
