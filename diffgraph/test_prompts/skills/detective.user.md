---
# Baseline detective — NO skill mounted. Detective has only the
# bare whereabouts/evidence/motive/text_answer/done tools and must
# track concerns + hypotheses in working memory across the
# investigation. Used by SKILL-003-without scenario as the
# reference behavior.
data:
  briefing:
    type: string
    description: "Short narrative of the crime — the setup the detective derives concerns from."
---
## Case briefing

{{ briefing }}

## Your task

Run the investigative loop described in the methodology — derive
concerns from the briefing, form hypotheses about who's
responsible, probe selectively to narrow the candidate set, and
accuse the unique match. Report via
`text_answer(text="<name>")`, then call `done(findings=[])`.
