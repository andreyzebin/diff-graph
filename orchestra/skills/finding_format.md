---
# Skill: finding_format
#
# Pure-prose skill — describes the finding-dict shape that BOTH
# `done(findings=[...])` (investigator output) and
# `pr_post_comment(...)` (reviewer publishing) share. Severity
# rubric is bundled because the severity FIELD is part of the
# shape, and the right way to assign it has the same logic
# (calibrate against consequence, not symptom visibility).
#
# Verdict-mapping ("BLOCKER/MAJOR → NEEDS_WORK; only MINOR/
# COMMENT → APPROVED") is reviewer-specific (it's about
# `set_review_status`, which only the reviewer holds), so it
# stays inline in reviewer.system.md rather than bundling here.
description: >-
  The finding-dict shape every agent that produces findings
  must use (file / line / severity / title / explanation /
  evidence / suggestion) plus the severity rubric — what each
  level means and how to calibrate against consequence rather
  than symptom visibility. Used by investigator (via
  `done(findings=[...])`) and reviewer (via
  `pr_post_comment` translation).
tools: []
---
## Finding shape

Every finding — whether returned by an investigator via
`done(findings=[...])` or posted by a reviewer via
`pr_post_comment(...)` — has the same fields:

- `file` — relative path.
- `line` — most relevant line in the changed code (use **new**
  coordinates — see the diff_view skill).
- `severity` — `BLOCKER` | `MAJOR` | `MINOR` | `COMMENT` (rubric
  below).
- `title` — one-line summary, ≤ 80 chars.
- `explanation` — what's wrong and why it matters (2–4 sentences).
- `evidence` — code or text that supports the finding (a quoted
  snippet, a thread id, a doc citation).
- `suggestion` — *(optional)* concrete fix as plain text, NOT a
  code block.

## Severity rubric

- **BLOCKER** — correctness bug, data corruption, security
  vulnerability.
- **MAJOR** — likely bug, bad pattern that will cause issues in
  practice.
- **MINOR** — suboptimal, worth fixing, not broken.
- **COMMENT** — style, naming, optional improvement.

Calibrate severity against the **consequence** you describe,
not the visibility of the symptom. *"Masks data integrity"*,
*"silent failure"*, *"hides root cause"*, *"authorization
bypass"*, *"data corruption"* — that's at least MAJOR, often
BLOCKER, no matter how small the textual change. A one-line
change can carry a BLOCKER finding.

The reverse miscalibration also bites: a strong consequence in
the explanation paired with a softened severity, hoping to avoid
blocking the PR. Severity follows consequence; verdict follows
severity.
