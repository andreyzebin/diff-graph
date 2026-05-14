---
# Continuation-review prompt: an earlier reviewer flagged a real
# issue; the author's latest commit addressed it. The agent's job is
# to confirm the fix landed (reply in the existing thread) and
# APPROVE — NOT to re-raise the original concern as a fresh inline
# finding. Thread reading is part of the reviewer's base surface
# (reviewer.system.md); tools_add here is just publishing + verdict
# — no spawn, so the test stays unit-isolated.
tools_add:
  - post_comment
  - set_review_status

# Same interface contract as production reviewer.user.md — the
# unit-test PR carries the same Bitbucket-PR data.
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

Review this PR.

**Step 1 — read existing threads.** Call `list_threads()`, then
`read_thread()` on every thread that anchors to a file in the
diff. Existing comments may have raised real issues the author has
since addressed.

**Step 2 — for each thread, compare timestamps:**

- The thread header carries `· YYYY-MM-DDThh:mmZ` after the
  author name.
- The `Commits` block above lists each commit's UTC timestamp
  next to its SHA, oldest first.
- If a commit landed **after** a thread's last comment, the issue
  the thread raised may be fixed.

**Step 3 — read the diff for the affected file** to verify whether
the latest commit actually addressed the issue.

**Step 4 — reply in the thread** with the verdict:

- If the issue is fixed by a later commit: leave a one-line
  `post_comment(text="Addressed in <sha7> — <short why>",
  parent_id=<root_id>)`.
- If the issue still stands: leave a one-line confirmation reply
  instead of posting a fresh inline finding.

**Do NOT** post a fresh inline `post_comment(file, line, ...)`
about an issue that is already raised in an existing thread —
even a refuted or addressed one. The reply in the thread is the
canonical channel for "I agree" / "fixed".

**Step 5 — set the verdict.** `set_review_status(APPROVED, reason)`
when every prior concern is addressed and you have no novel
findings. `NEEDS_WORK` when at least one concern still stands or
you found a fresh blocker. Then `done(findings)`.
