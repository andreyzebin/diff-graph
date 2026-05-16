---
# Budget-aware delegation variant. The reviewer sees its run state
# (budget + time + subagents) on EVERY reflect call — opted into via
# `reflect_response_template: with_state`. No separate budget_stats
# query needed; the snapshot arrives in the reflect tool-result, so
# the reviewer plans on fresh data every reflection cycle.
#
# Delegation criterion is BUDGET-PRESSURE rather than cross-source
# (per TODO §13.10 — cross-source rationale gives zero real benefit
# until §10 Phase D ships investigator-extended tools). The
# reviewer should anticipate context pressure and offload reading
# into child investigators (fresh windows) rather than waiting for
# NUDGE_HIGH and reacting too late.
#
# Production reviewer.user.md stays untouched until this shape
# proves itself.
reflect_response_template: with_state
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

**Read the ticket** via `jira_read_ticket(ref)` (copy each ref
verbatim from the list above), then **orient on the diff** with
`diff_list_files`.

**Decide per concern using budget-pressure rationale.** For each
concern this PR raises:

- **Project your reading cost vs your context headroom.** Look at
  the most recent reflect's state snapshot. Each `diff_read_file`
  adds the file's full size to your own context; an
  `agent_spawn(agent="investigator", focus="...")` instead returns
  a brief ~3-5K summary while the child reads in its own fresh
  window.
- **If reading all material yourself would push you past ~75% of
  your context window**, spawn investigators for at least some
  concerns rather than read everything yourself. Tight windows
  make direct investigation risky — you may run out of headroom
  mid-synthesis.
- **If your context is comfortable** AND the concern is small
  (one file, one function), note it directly — no spawn needed.

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
