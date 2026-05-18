---
# Skill: reflect
#
# Bundles the `reflect` tool with guidance on its cognitive role:
# a convergence aid for investigative multi-step problems. Forces
# the agent to externalise its working state — established facts,
# open questions (with stable IDs), questions just resolved,
# current confidence, next concrete action — at decision
# boundaries between steps.
#
# History: extracted from `register_builtins` so that agents
# whose tasks don't need state-banking (single-step responders,
# mechanical pipelines) don't carry the cognitive overhead by
# default. Investigative agents (reviewer, investigator,
# dispatcher in /ask mode) mount it explicitly.
#
# Why this is a skill, not just a tool: the tool by itself is
# inert — what makes reflect useful is the discipline of WHEN to
# call it (state-change checkpoints, not narration) and WHAT to
# put in each field (justified facts, branching questions with
# resolution paths, not free-form journaling). The skill body
# carries that contract. Production agents that mount this skill
# get the tool + the contract atomically.
description: >-
  Convergence aid for investigative multi-step problems. Externalises
  the agent's working state — established facts, open questions with
  stable IDs, just-resolved questions, current confidence, and the
  next concrete action — at decision boundaries between steps.
  Without this externalisation, models lose partial findings in long
  chains, re-ask resolved questions, and skip back to working memory
  as the sole state store. Mount this skill when the agent's task
  involves building an answer up from multiple sub-results,
  tracking what's ruled out vs. still in play, or choosing between
  branches where the choice matters.
# Default cadence: nudge after 5 substantive steps without a
# reflect. Agents that need a tighter or looser rhythm can
# override via their own `reflect: { interval: N }` block — the
# mount_skills merge uses `setdefault`, so the prompt's existing
# key wins. `with_state: true` is intentionally NOT set here: it
# re-injects budget + sub-agents snapshot into the reflect tool's
# return, which only earns its keep when the agent makes runtime-
# context-dependent decisions (spawn-vs-direct). prefer_delegation
# declares it for that reason; mounting both composes correctly
# (skill setdefault doesn't clobber).
reflect:
  interval: 5
tools:
  - reflect
---
**reflect is a convergence aid, not narration.** It exists to
help you stop losing state across steps in multi-step
investigations. Each call is a checkpoint that re-anchors your
model of the world; the field structure forces you to
*externalise* what you actually know, what you don't, and why
your next action is the right one.

**Field-by-field contract:**

- `learned` — facts you can now treat as established. One line
  per fact; keep it tight. Numeric bounds you've narrowed,
  entities you've identified, branches you've ruled out. If you
  can't state a fact in one line, you don't really know it yet.
- `questions_remaining` — what you still need to answer to make
  the NEXT decision (not the whole investigation). Each gets a
  stable short ID (`Q1`, `Q2`…). The IDs are how you resolve
  later — they let you say "Q1: dropped, Q3: answered" without
  re-typing prose.
- `resolved_questions` — questions from your PREVIOUS reflect
  that you can now close. Reference by ID; put the concrete
  answer in `summary`. Each closure is a progress signal — the
  ratio of closed-vs-still-open over reflects tells the budget
  layer whether you're converging or spinning.
- `confidence` — `low` / `medium` / `high` honestly. Don't claim
  `high` while a load-bearing question is still open. Drift
  between confidence and questions_remaining is a smell.
- `next_action` — the single concrete step you're about to take,
  justified against `learned`. Not a plan tree, not a list of
  options. One action, one reason.

**When to call reflect:**

- After a tool call that changed your model of the world (read
  returned, probe came back, search found / missed) — and BEFORE
  deciding the next call.
- Before switching direction (give up on branch A, start B).
  Externalising "Q-for-A: dropped, opening Q-for-B" makes the
  switch legible and reversible.
- When you notice yourself about to repeat work or guess —
  banking forces you to state what you actually know, which
  usually surfaces that you already had the answer.
- At long-chain checkpoints (every 3-5 substantive steps) even
  without an obvious trigger — drift is silent and reflect is
  the only thing that catches it before context grows past
  recovery.

**When NOT to call reflect:**

- Trivial follow-throughs where the next step is mechanically
  obvious from the last (parsed response → fill template →
  submit; one-step lookup → answer).
- Single-tool tasks (one probe → answer; one read → quote).
  Reflect's overhead doesn't earn its keep on these.
- Pure narration ("I'm about to call X then Y then Z"). If your
  reflect would just paraphrase your next three actions, skip
  it — the value is in state-banking, not commentary.

**Failure modes reflect prevents:**

- **Loops** — re-asking a question already resolved in an earlier
  reflect (resolved_questions makes the closure explicit;
  re-opening the same ID is a flag).
- **Drift** — partial findings crowded out of working memory as
  context grows (learned anchors them as plain text the next
  step's prompt sees verbatim).
- **Premature termination** — giving up because state went fuzzy
  and "I don't know if I'm making progress" (confidence +
  resolved/remaining ratio give an honest answer).
- **Unjustified branch switches** — abandoning a thread without
  saying why (questions_remaining with explicit `dropped`
  resolutions force the decision into the open).
