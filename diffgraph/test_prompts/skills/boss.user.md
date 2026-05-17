---
# Baseline boss — NO skill mounted. Boss is expected to run
# do_task itself and report. Used by SKILL-001-without scenario
# as the reference behavior.
data:
  task_input:
    type: string
    description: "Numeric input the task wants computed."
---
Compute the result for input = {{ task_input }}. Report ONLY the
resulting number via `text_answer(text="N")`, then call
`done(findings=[])`.
