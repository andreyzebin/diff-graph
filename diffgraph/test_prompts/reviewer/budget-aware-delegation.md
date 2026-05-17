---
# Budget-aware delegation variant. The reviewer sees its run state
# (budget + time + subagents) on EVERY reflect call — opted into via
# `reflect_response_template: with_state`. No separate budget_stats
# query needed; the snapshot arrives in the reflect tool-result, so
# the reviewer plans on fresh data every reflection cycle.
#
# Delegation rationale is now a SKILL — `prefer_delegation`
# (orchestra/skills/prefer_delegation.md). The skill
# bundles agent_spawn + agent_list with the positive depth-as-
# upgrade rationale. Abstract over delegate names — the skill text
# talks about "the right delegate" rather than naming `investigator`,
# so the same skill works for any orchestrator-role agent that
# delegates to specialised subagents.
#
# History: TODO §13.10b/c — positive framing landed after B+C
# (can-I-answer-now + breadth) and pure budget-pressure both failed
# to push deepseek-chat off direct reads. Plan 289 confirmed
# end-to-end delegation + synthesis through semantic mocks.
#
# Tight `max_context` (16K) stays as a backstop while we calibrate.
# Production reviewer.user.md stays untouched until this shape
# proves itself on the bench across providers.
#
# `reflect_response_template: with_state` is supplied by the
# prefer_delegation skill itself — no need to redeclare here.
# The skill says "I need every reflect to carry a live budget
# snapshot" so the agent can price spawn-vs-direct on each
# planning moment.
budget:
  max_context: 16000
skills:
  - prefer_delegation
tools:
  - text_answer        # scenario-specific deliverable channel
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
  jira_tickets:
    type: string
    description: "Jira ticket reference(s) this PR is associated with"
---
PR: {{ pr.title }}
{{ pr.description }}

Commits *(oldest → newest)*:

{{ pr.commits }}

This PR is associated with these Jira ticket(s): {{ pr.jira_tickets }}

**Every reflect call returns your run state.** Each `reflect(...)`
tool-result carries the current snapshot: time, your own context
window usage, the shared pool with children, any spawned subagents,
and a "typical investigator spawn" cost reference. Re-plan each
time you reflect — your situation has changed.

{{ budget_stats_legend }}

**Read the ticket** via `jira_read_ticket(ref)` (copy each ref
verbatim from the list above), then **orient on the diff** with
`diff_list_files`.

{{ skills }}

**Synthesize.** When all spawns have returned, compile the final
concerns list. Submit via `text_answer(text=...)`, one concern per
line as `- <short title>: <one-sentence question>`. Cite the ticket
AC where relevant. No preface, no summary, no fix suggestions.
Then call `done(findings=[])`.

Use `reflect` actively — both as private working memory AND as your
state-refresh checkpoint (the snapshot in its tool-result is your
authoritative view of where you are).
