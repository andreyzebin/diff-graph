---
# Interface contract — the Bitbucket-PR-comment invocation surface.
# A different interface (CLI, Slack, …) would redeclare this block.
data:
  message:
    type: string
    description: "full user message; may contain /command or plain text. Empty when no PR-comment context."
  comment_id:
    type: integer
    description: "invoking comment ID. 0 = no comment context (CLI / auto-trigger / benchmark)."
  comment_thread:
    type: string
    description: "thread from root to invoking comment, or '(no thread)' when comment_id is 0."
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"

guards:
  # post_comment is interface-specific — guard message lives with the
  # interface, not the methodology.
  require_tool:post_comment: "You stopped without replying. The user can only see post_comment() output. Call post_comment(text=..., parent_id={comment_id}) once, then finish with done()."
---
# Trigger

- `COMMENT_ID`: `{comment_id}`
- `PR`: **{pr_title}** — {pr_description}

## Thread

{comment_thread}

## Message

{message}

If you need to see what *other* threads exist on this PR — only when
the trigger genuinely calls for cross-thread context, e.g. an /ask
that explicitly references prior discussion — call `list_threads()`
to orient yourself, then `read_thread(<id>)` to drill in. For a
greeting, a /help, or a /review command, you do not need other
threads.
