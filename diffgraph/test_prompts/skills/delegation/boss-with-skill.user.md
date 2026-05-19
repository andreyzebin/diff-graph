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
    description: "Comma-separated list of integer inputs to compute, e.g. '21, 7, 100, 5, 33'."
---
Compute the result for each input in this list: {{ task_input }}.

Submit the results as a single comma-separated answer via
`text_answer(text="N1, N2, N3, N4, N5")` — in the SAME ORDER as
the inputs — then call `done(findings=[])`.
