---
# Baseline boss — NO skill mounted. Used by SKILL-001-without
# scenario as the reference behavior. Task body is IDENTICAL to
# boss-with-skill.user.md (no nudge toward delegation in the
# prompt); the only difference is `skills:` in the with-variant
# frontmatter.
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
