---
# Baseline cluedo player — NO skill mounted. The player has only
# the bare suggest/text_answer/done tools and must track eliminated
# cards + opponent attribution + per-category candidate sets in
# working memory across the multi-suggestion deduction. Used by
# SKILL-004-without scenario as the reference behavior.
data: {}
---
Identify the three cards in the envelope (one suspect, one
weapon, one room) by issuing `suggest()` calls and reasoning
about the responses. Apply the constraint-propagation rules from
the methodology. When you've uniquely pinned all three, report
via `text_answer(text="<suspect>, <weapon>, <room>")` and then
call `done(findings=[])`.
