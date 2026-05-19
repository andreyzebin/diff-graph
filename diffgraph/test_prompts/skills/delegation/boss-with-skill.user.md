---
# Skill-mounted boss — prefer_delegation skill adds agent_spawn +
# agent_list and shifts strategy to default-delegate. Used by
# SKILL-001-with scenario; A/B partner of boss.user.md.
skills:
  - prefer_delegation
data:
  task_input:
    type: string
    description: "Numeric input the task wants computed."
---
Compute the result for input = {{ task_input }}. Report ONLY the
resulting number via `text_answer(text="N")`, then call
`done(findings=[])`.
