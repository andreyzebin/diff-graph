---
# Skill: reflect
#
# Bundles the `reflect` tool with guidance on how to use it for
# progressive multi-step reasoning — banking established facts,
# tracking open questions with stable IDs, resolving them as
# answers arrive, and naming the next concrete action.
#
# Provisional stub — this skill exists to let SKILL-002 bench
# scenarios A/B-test "reflect available vs absent" before the
# eventual extraction of `reflect` from `register_builtins` to
# the skill layer. Body is the minimum useful guidance; tools
# block makes the bare reflect tool available without the
# `with_state: true` state-rendering toggle. Skills that want the
# state snapshot (e.g. prefer_delegation) declare it independently.
description: >-
  Banks the agent's progressive reasoning state: facts established
  so far, questions still open (with stable IDs), questions just
  resolved, current confidence, and the next concrete action.
  Without it, agents must keep all state in working memory between
  steps — fine for short tasks, lossy for long ones.
tools:
  - reflect
---
**Reflect to drive progressive search.** Between non-trivial
steps, call `reflect(...)` to bank what changed in your model
of the world:

- `learned` — facts you can now treat as established (numeric
  bounds, identified entities, ruled-out branches). One line per
  fact; keep it tight.
- `questions_remaining` — what you still need to answer to
  make the NEXT decision. Each gets a stable short ID (`Q1`,
  `Q2`…) so you can resolve it by ID on a later reflect rather
  than retyping the prose.
- `resolved_questions` — questions from your PREVIOUS reflect
  that you can now answer. Reference by ID; include the
  concrete answer in `summary`.
- `confidence` — `low` / `medium` / `high` honestly. Don't claim
  `high` while a question is still open.
- `next_action` — the single concrete step you're about to take,
  not a plan tree.

**When to call reflect:**

- After each tool call that changed your state of the world
  (read returned, probe came back, search found / missed) — and
  before deciding the NEXT call.
- Before switching direction (give up on branch A, start B).
- When you notice yourself about to repeat work or guess —
  banking forces you to state what you actually know.

**When NOT to call reflect:** trivial follow-throughs where the
next step is mechanically obvious from the last (a parsed
response that lands directly in your final answer, a one-step
lookup). Reflect should be a state-change checkpoint, not
narration.
