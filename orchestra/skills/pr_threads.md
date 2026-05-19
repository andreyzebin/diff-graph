---
# Skill: pr_threads
#
# Bundles the three on-demand PR-thread reading tools with the
# common methodology for using them — "look only when relevant,
# don't dup what's already raised". Used by reviewer +
# investigator (drilling into prior discussion to dedup against
# open concerns) and dispatcher (occasional cross-thread peek
# when a trigger references other context).
#
# Agents that have a stricter discipline (the dispatcher's "do
# not look by default — cross-thread drift is the #1 failure
# mode") can layer their own inline guidance AFTER the skill
# block — this skill carries the common foundation, not the
# per-agent anti-drift rules.
description: >-
  On-demand reading of the PR's existing comment threads:
  `pr_list_threads` for orientation, `pr_read_thread` to drill
  into one root, `pr_read_comment` for a single body when
  `pr_read_thread` truncated. Includes the dedup-against-open
  rule and the snapshot-at-run-start semantic.
tools:
  - pr_list_threads
  - pr_read_thread
  - pr_read_comment
---
## Existing PR discussion

The PR may have prior comments and threads. They are NOT in your
prompt — fetch them on demand via tools:

- `pr_list_threads(start, n, sort, repo="default", pr="default")` —
  orientation: one-line summary per root thread (id, author,
  reply count, first line of the root body).
- `pr_read_thread(comment_id, repo="default", pr="default")` —
  full thread containing `comment_id`, depth-first from the
  root. Pass any id in the thread; the tool finds the root and
  walks the subtree. Long bodies / deep trees truncate with a
  hint to call `pr_read_comment` or `pr_read_thread` on a
  sub-id.
- `pr_read_comment(comment_id, repo="default", pr="default")` —
  one specific comment in full, no caps. Use when
  `pr_read_thread` truncated a body you need.

**When to look.** When the work you're about to do could
plausibly overlap with an existing thread — dedup against open
concerns, reply to a thread your finding touches, react to one
you agree with. Default is **don't look** when the task is
clearly novel; the discussion graph has cost and is rarely the
critical path.

**Snapshot semantics.** The thread snapshot is fixed at run
start. Your own outputs during this run (posted comments,
reactions, status changes) are not visible to subsequent reads —
so the tools don't show you your own work-in-progress, and you
can't rely on a read to "see" what you just wrote. Cite a
thread in your finding's `evidence` directly with the id rather
than asking a subsequent read to confirm it.
