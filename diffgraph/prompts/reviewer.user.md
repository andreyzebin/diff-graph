---
# End-to-end production review — extend the base diff toolkit with
# thread reading, delegation, publishing, and verdict.
tools_add:
  - list_threads
  - read_thread
  - read_comment
  - react_to_comment
  - list_agents
  - spawn_agent
  - post_comment
  - set_review_status

# Interface contract — Bitbucket-PR data the reviewer receives
# from its parent (dispatcher) at spawn time.
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

Commits *(oldest → newest)*:

{commits}

Review this PR end-to-end.

**Read existing threads first.** Call list_threads(), then
read_thread() on anything that looks relevant to the diff.
Knowing what's already been raised changes what counts as a "new"
finding — duplicating an open thread is noise, not signal.

Then read the diff and identify concerns. Spawn investigators
(spawn_agent) for any concern that needs depth. If you're unsure
which agent name to spawn, call list_agents() once to see the
registry.

**Match each concern against the threads you read.** Three outcomes:

- **Already raised, still open** — don't post a new comment. If the
  thread is unanswered and your evidence either confirms or refutes
  the original point, leave a reply with `post_comment(text=...,
  parent_id=<root_comment_id>)` updating the status. If you simply
  agree with what's already there, react with `react_to_comment(...,
  emoticon=thumbs_up)` instead of replying.
- **Already raised, already resolved** — leave it alone (or
  `thumbs_up` the resolution if it's a good one).
- **Novel** — publish via `post_comment(file, line, severity, text)`
  as a fresh inline finding.

After every concern is either published, replied to, or skipped,
set the verdict via `set_review_status(APPROVED|NEEDS_WORK, reason)`
and finish with `done(findings)`.
