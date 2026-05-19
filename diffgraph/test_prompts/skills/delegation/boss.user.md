---
# Baseline boss — NO skill mounted. Used by SKILL-001-without
# scenario as the reference behavior. Boss is expected to call
# do_task itself for each input (sequential is fine — no skill
# pushing for parallel fan-out).
data:
  task_input:
    type: string
    description: "Comma-separated list of integer inputs to compute, e.g. '21, 7, 100, 5, 33'."
---
Compute the result for each input in this list: {{ task_input }}.

Submit the results as a single comma-separated answer via
`text_answer(text="N1, N2, N3, N4, N5")` — in the SAME ORDER as
the inputs — then call `done(findings=[])`.
