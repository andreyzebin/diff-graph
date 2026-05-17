---
# Budget-aware delegation variant. The reviewer sees its run state
# (budget + time + subagents) on EVERY reflect call — opted into via
# `reflect_response_template: with_state`. No separate budget_stats
# query needed; the snapshot arrives in the reflect tool-result, so
# the reviewer plans on fresh data every reflection cycle.
#
# Delegation rationale is COVERAGE-MAXIMIZATION + per-concern
# "can-I-answer-now?" triage (see §13.10 update).
#
#   Goal reframe (C): reviewer's deliverable is BREADTH of concerns
#   surfaced, not depth on any single one. Each investigator's
#   done() summary returns ~3-5K to the parent's synthesis window,
#   so the parent can hold 10+ in parallel. Direct file reading is
#   a depth-first scan of one concern; spawning is breadth-first
#   across all of them. The reviewer is a router; the investigator
#   is a digger.
#
#   Per-concern rule (B): for each concern this PR might raise,
#   apply this plan-time test: "can I answer from what I already
#   have — ticket AC + diff lines + existing PR comments — without
#   reading any unread file?" Yes → note directly. No → spawn.
#   `diff_read_file` is allowed only to verify a SPECIFIC line the
#   diff already highlighted — never for exploration.
#
# Why this beats the budget-pressure rationale we tried first:
# budget pressure is a state observed mid-flight, not a rule the
# agent can apply at planning time. By the time the snapshot
# shows 75%, the reviewer has already committed to direct reads.
# The new rule fires the moment a concern is formulated, before
# any read happens.
#
# Tight `max_context` (16K) stays as a backstop — even with the
# coverage framing, a small context discourages "I'll just peek
# at one more file" drift. With production 128K the rule still
# applies but the safety net is gone.
#
# Production reviewer.user.md stays untouched until this shape
# proves itself.
reflect_response_template: with_state
budget:
  max_context: 16000
tools_add:
  - agent_spawn
  - agent_list
  - text_answer
extra_tools:
  - name: text_answer
    description: "Submit your final concerns list. Call once at the end with the full list as `text`. The agent's only deliverable channel for this task."
    parameters:
      type: object
      properties:
        text:
          type: string
          description: "Concerns list, plain text, one per line as `- <short title>: <one-sentence question>`."
      required:
        - text

# Interface contract — same Bitbucket-PR shape as concerns-text, plus
# jira_tickets so the reviewer can ground concerns in the ticket AC
# before delegating.
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
    description: "Jira ticket reference(s) this PR is associated with"
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

This PR is associated with these Jira ticket(s): {jira_tickets}

**Every reflect call returns your run state.** Each `reflect(...)`
tool-result carries the current snapshot: time, your own context
window usage, the shared pool with children, any spawned subagents,
and a "typical investigator spawn" cost reference. Re-plan each
time you reflect — your situation has changed.

{budget_stats_legend}

**Read the ticket** via `jira_read_ticket(ref)` (copy each ref
verbatim from the list above), then **orient on the diff** with
`diff_list_files`.

**Your deliverable is BREADTH of concerns, not depth on any
single one.** Each investigator's `done()` returns ~3-5K to your
synthesis window — you can hold 10+ in parallel. Direct file
reading is a depth-first scan of one concern; spawning
investigators is a breadth-first scan across all of them.
Reviewer = router; investigator = digger.

**Per-concern triage.** For each concern this PR might raise,
apply this test BEFORE any file read:

> *Can I answer this from what I already have — the ticket AC,
> the diff lines themselves, and any existing PR comments —
> without reading any unread file?*

- **Yes** → note the concern directly. No spawn.
- **No** → `agent_spawn(agent="investigator", focus="<the
  specific question>")` and let the child read in its own fresh
  window.

`diff_read_file` is allowed ONLY to verify a SPECIFIC line the
diff already highlighted — never for exploration of an unread
file. If you find yourself reaching for it to "go check
something" or "see how it's used elsewhere", that's a spawn
instead. Same for `diff_search` and `diff_outline` against files
outside the diff — spawn instead of looking yourself.

Multiple spawns in one step run in parallel. Use `agent_list()` if
unsure which agent to spawn.

**Synthesize.** When all spawns have returned, compile the final
concerns list. Submit via `text_answer(text=...)`, one concern per
line as `- <short title>: <one-sentence question>`. Cite the ticket
AC where relevant. No preface, no summary, no fix suggestions.
Then call `done(findings=[])`.

Use `reflect` actively — both as private working memory AND as your
state-refresh checkpoint (the snapshot in its tool-result is your
authoritative view of where you are).
