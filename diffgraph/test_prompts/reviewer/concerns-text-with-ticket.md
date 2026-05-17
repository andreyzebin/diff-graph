---
# Ticket-backed concerns variant of concerns-text.md. Same
# concerns-text deliverable channel; `jira_read_ticket` is in the
# reviewer's base surface (reviewer.system.md), so tools_add here
# only carries the text_answer capture tool. The prompt body below
# tells the reviewer the PR's associated Jira ticket(s) and nudges
# it to ground concerns in the ticket's acceptance criteria rather
# than guessing intent from the diff alone.
tools:
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

# Interface contract — same Bitbucket-PR shape as production
# reviewer.user.md, plus jira_tickets (the PR's associated ticket
# refs; in production pr_context resolves these from Bitbucket's
# Jira-integration endpoint, here the scenario's agent_data
# supplies them).
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
PR: {{ pr_title }}
{{ pr_description }}

Commits *(oldest → newest)*:

{{ commits }}

This PR is associated with these Jira ticket(s): {{ jira_tickets }}

**Read the ticket(s) first.** Call `jira_read_ticket(ref)` on each
associated ticket before forming concerns — copy the ref verbatim
from the list above. The ticket carries the acceptance criteria the
diff is supposed to satisfy; a concern grounded in "the ticket's AC
says X, the code does Y" is sharper and more actionable than the
same observation made from the diff alone. If a ticket links to an
epic or sibling tickets and the broader effort changes how you'd
weigh a finding, read those too (`jira_read_ticket` on the linked key).
If `jira_read_ticket` says the ticket is unreachable, proceed with the
diff + PR description alone — don't retry in a loop.

Then identify the concerns this diff raises and submit them via
`text_answer(text=...)`. Use reflect during your work as private
working memory — facts learned, hypotheses still open, what's
resolved — but the run's deliverable is the text you pass to
text_answer at the end. Then call done(findings=[]).

text_answer payload shape: plain text, one concern per line as
`- <short title>: <one-sentence question>`. Where a concern is
grounded in the ticket, say so (cite the AC). No preface, no
summary, no fix suggestions — just the concerns list.
