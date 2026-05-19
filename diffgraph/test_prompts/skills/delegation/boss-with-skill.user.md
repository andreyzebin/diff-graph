---
# Skill-mounted boss — prefer_delegation skill adds agent_spawn +
# agent_list. Task body is IDENTICAL to boss.user.md (no nudge
# toward delegation in the prompt); the skill body is the ONLY
# difference. Used by SKILL-001-with scenario.
skills:
  - prefer_delegation
data:
  task_input:
    type: string
    description: "Comma-separated list of integer inputs to compute."
---
Compute the result for each of the following inputs:

  {{ task_input }}

Submit the results as a single comma-separated answer via
`text_answer(text="N1, N2, …")` — in the SAME ORDER as the
inputs — then call `done(findings=[])`.
