---
# End-to-end production review — extend the base diff toolkit with
# thread reading, delegation, publishing, and verdict.
tools_add:
  - list_threads
  - read_thread
  - read_comment
  - list_agents
  - spawn_agent
  - post_comment
  - set_review_status
  # NB: react_to_comment intentionally omitted — Bitbucket Server
  # doesn't expose a public reactions REST endpoint (returns 404),
  # so reactions silently fail. Use post_comment(parent_id=...) for
  # both "agree" replies and "disagree" follow-ups.

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

**Match each concern against the threads you read.** Four outcomes:

- **Already raised, no [SELF] reply yet** — leave a brief
  `post_comment(text=..., parent_id=<root_comment_id>)` saying
  whether your evidence confirms or refutes the original point.
  One line is enough.
- **Already raised, you already replied [SELF]** — check the
  timestamp on your reply against the latest commit shown in
  `Commits` above. If the latest commit is **at or before** your
  reply, leave it alone — your prior confirmation still holds and a
  duplicate is noise. If commits landed **after** your reply, post
  a fresh update reply: confirm the finding still stands, or note
  that the new commits addressed it.
- **Already raised, already resolved** — leave it alone. Don't
  re-litigate.
- **Novel** — publish via `post_comment(file, line, severity, text)`
  as a fresh inline finding.

Thread comments now carry an ISO timestamp in the header
(`=== #<id> by <author> · YYYY-MM-DDThh:mmZ · ...`) so you can
compare reply age vs commit dates without external help. The
header's anchor clause (`@ <path>:<line>@<sha7>`) tells you which
commit the inline anchor pegs to, and `(outdated)` appears when
Bitbucket detected the anchored line was removed by subsequent
commits — that's a strong "this discussion may not still apply"
signal. Treat outdated threads the same as already-resolved.

After every concern is either published, replied to, or skipped,
set the verdict via `set_review_status(APPROVED|NEEDS_WORK, reason)`
and finish with `done(findings)`.
