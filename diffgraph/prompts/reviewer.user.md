---
# End-to-end production review — the base surface (diff reading +
# thread reading) is in reviewer.system.md; tools_add here is the
# acting-on-outputs surface: delegation, publishing, and verdict.
tools_add:
  - agent_list
  - agent_spawn
  - pr_post_comment
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
  jira_tickets:
    type: string
    from: pr_context.jira_tickets
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

Linked Jira ticket(s): {jira_tickets}

Review this PR end-to-end.

**Read existing threads first.** Call pr_list_threads(repo="default", pr="default"),
then pr_read_thread(<id>, repo="default", pr="default") on anything that looks relevant to the diff.
Knowing what's already been raised changes what counts as a "new"
finding — duplicating an open thread is noise, not signal.

**Glance at the linked Jira ticket.** The "Linked Jira ticket(s)"
line above is resolved authoritatively from Bitbucket — if it lists
one or more `handle/namespace/key` refs, call jira_read_ticket(ref) on
each (copy the ref verbatim) before forming concerns. If it's empty
or "(none)", check the title / branch name / commits for a ticket
key yourself and jira_read_ticket that; if "(unavailable)", skip it. The
ticket carries the acceptance criteria the diff is supposed to
satisfy: a concern grounded in "the ticket's AC says X, the code
does Y" is sharper and more actionable than the same observation
made from the diff alone. If a ticket links to an epic or sibling
tickets and the broader effort changes how you'd weigh a finding,
read those too (jira_read_ticket on the linked key). jira_read_ticket
degrades gracefully — if it comes back "disabled" / "not
configured" / "could not be read", that's fine, just proceed with
the diff + PR description; don't retry it in a loop.

Then read the diff and identify concerns. Spawn investigators
(agent_spawn) for any concern that needs depth. If you're unsure
which agent name to spawn, call agent_list() once to see the
registry.

**Continuation review — scope to only the unseen commits.** If
`pr_list_threads(repo="default", pr="default")` shows at least one `[SELF in subtree]` thread,
you've reviewed this PR before. Treat the prior review as the
baseline: only the commits that landed **after** your last [SELF]
reply timestamp are new material worth re-reading.

How to scope reads to the unseen delta:

1. Pick `<last_seen_sha>` — the latest commit from the `Commits`
   block above whose timestamp is **at or before** your last
   [SELF] reply timestamp. That's the SHA the existing review
   already covered.
2. For every subsequent `diff_read_file` / `diff_outline` /
   `diff_search` call on this run, pass `ref="<last_seen_sha>..source"`
   instead of the default. The VFS materialises only the changes
   between that commit and the source tip — your re-read focuses
   on truly new code, not the whole PR.
3. New concerns from this delta get the normal four-outcome
   matching below.

If there are no [SELF] threads, this is the first pass — use the
default `ref` (whole PR) and review everything.

**Match each concern against the threads you read.** Four outcomes:

- **Already raised, no [SELF] reply yet** — leave a brief
  `pr_post_comment(text=..., parent_id=<root_comment_id>, repo="default", pr="default")` saying
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
- **Novel** — publish via `pr_post_comment(file, line, severity, text, repo="default", pr="default")`
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
