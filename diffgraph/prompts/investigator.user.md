---
# `reflect` skill is mounted at the agent level in
# investigator.system.md — every user prompt for this agent gets
# it. Per-call user prompts can mount ADDITIONAL skills via their
# own `skills:` frontmatter; system + user lists union at
# Agent.__init__.
#
# Interface contract — data the investigator receives at spawn time.
data:
  commits:
    type: string
  focus:
    type: string
    description: "high-level concern to investigate (from lead)"
---
# Your concern

{{ focus }}

# Commits *(oldest → newest)*

{{ pr.commits }}

# Task: investigate the concern above

Workflow:

1. **Start** by reading changes for files relevant to your concern:
   `diff_read_file(path, changes_only=true, before=3, after=3, repo="default")`
   for each file from *What changed* that relates to your focus.
   This shows only diff hunks. Also call
   `diff_outline(path, repo="default")` on key files. Gather facts
   before forming concrete hypotheses.

2. **Investigate** with the diff tools. Follow call chains, check
   related code, verify assumptions.

3. **Optional dedup against existing PR discussion** — if your
   focus could plausibly be already raised, call
   `pr_list_threads(repo="default", pr="default")` once and
   `pr_read_thread(<id>, repo="default", pr="default")` on anything
   that looks related. Don't re-report issues already covered by
   an open thread; cite the thread in your finding's `evidence`
   if relevant. Skip entirely when the focus is clearly novel.

4. Call `done(findings)` when all questions are answered or
   budget is running low.

{{ skills }}
