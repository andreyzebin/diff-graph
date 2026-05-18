---
# Baseline searcher — NO skill mounted. Searcher has only the
# bare probe/text_answer/done tools and must converge in working
# memory. Used by SKILL-002-without scenario as the reference
# behavior.
data: {}
---
Find the hidden integer target in the inclusive range [1, 100].
You may call `probe(guess)` up to 10 times. Report ONLY the
final integer via `text_answer(text="N")`, then call
`done(findings=[])`.
