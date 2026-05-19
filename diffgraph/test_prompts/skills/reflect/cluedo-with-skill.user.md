---
# Skill-mounted cluedo player — `reflect` skill adds the
# `reflect` tool + guidance to bank the constraint state
# (eliminated cards per category, opponent attribution, remaining
# candidates) across the multi-suggestion deduction. The intended
# pattern: open Q-IDs per category ("Q1: which suspect?"), resolve
# them as cards get eliminated, escalate to candidate lists, and
# pin the unique combination via a final clean no_disproof or by
# full elimination. Used by SKILL-004-with scenario; A/B partner
# of cluedo.user.md.
skills:
  - reflect
data: {}
---
Identify the three cards in the envelope (one suspect, one
weapon, one room) by issuing `suggest()` calls and reasoning
about the responses. Apply the constraint-propagation rules from
the methodology. When you've uniquely pinned all three, report
via `text_answer(text="<suspect>, <weapon>, <room>")` and then
call `done(findings=[])`.
