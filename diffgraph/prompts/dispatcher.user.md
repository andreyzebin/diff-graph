---
# Interface contract — the Bitbucket-PR-comment invocation surface.
# A different interface (CLI, Slack, …) would redeclare this block.
data:
  message:
    type: string
    description: "full user message; may contain /command or plain text. Empty when no PR-comment context."
  comment_id:
    type: integer
    description: "invoking comment ID. `-1` (sentinel) = no comment context (CLI / webhook auto-trigger / benchmark); `0` accepted as a legacy fallback. Positive values are real Bitbucket comment ids."
  comment_thread:
    type: string
    description: "thread from root to invoking comment, or '(no thread)' when comment_id <= 0."
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"

guards:
  # pr_post_comment is interface-specific — guard message lives with the
  # interface, not the methodology.
  require_tool:pr_post_comment: "You stopped without replying. The user can only see pr_post_comment() output. Call pr_post_comment(text=..., parent_id={comment_id}, repo=\"default\", pr=\"default\") once, then finish with done()."
---
# Trigger

- `COMMENT_ID`: `{{ comment_id }}`
- `PR`: **{{ pr_title }}** — {{ pr_description }}

## Thread

{{ comment_thread }}

## Message

{{ message }}

If you need to see what *other* threads exist on this PR — only when
the trigger genuinely calls for cross-thread context, e.g. an /ask
that explicitly references prior discussion — call `pr_list_threads(repo="default", pr="default")`
to orient yourself, then `pr_read_thread(<id>, repo="default", pr="default")` to drill in. For a
greeting, a /help, or a /review command, you do not need other
threads.
