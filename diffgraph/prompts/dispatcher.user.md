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
