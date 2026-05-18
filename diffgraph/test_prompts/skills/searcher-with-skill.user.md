---
# Skill-mounted searcher — `reflect` skill adds the `reflect`
# tool + guidance to bank progressive state (range bounds, probes
# tried, open question for the next midpoint). Used by SKILL-002-
# with scenario; A/B partner of searcher.user.md.
skills:
  - reflect
data: {}
---
Find the hidden integer target in the inclusive range [1, 100].
You may call `probe(guess)` up to 10 times. Report ONLY the
final integer via `text_answer(text="N")`, then call
`done(findings=[])`.

{{ skills }}
