---
# Skill: prefer_delegation
#
# Bundles the tools an orchestrator-role agent needs to delegate
# (agent_spawn + agent_list) with rationale that makes delegation
# the DEFAULT action for any concern that warrants real
# investigation. Direct handling stays as the exception for
# trivially-visible concerns. Framed as a DEPTH upgrade (positive
# framing) — model sees spawning as "get the best answer" rather
# than "obey a prohibition on direct reads".
#
# Abstract over agent names: text refers to "the delegate" /
# "the right delegate" rather than naming `investigator`
# specifically. The actual delegate name is whatever the agent
# picks via agent_list() at runtime — could be investigator,
# researcher, auditor, etc.
#
# History: TODO §13.10c — positive framing landed after B+C
# (can-I-answer-now + breadth) and pure budget-pressure both
# failed to push deepseek-chat off direct reads.
description: >-
  Shifts the agent's execution strategy: each concern that needs
  real investigation is routed to the best-suited executor agent
  via agent_spawn, with the parent acting as router + synthesiser.
  Direct handling stays as the exception for trivially-visible
  concerns. Goal — maximise result quality within the run's
  shared budget by letting specialised delegates work in fresh
  context windows.
# This skill needs every reflect to carry a live budget + time +
# subagents snapshot — without it the agent can't price the
# spawn-vs-direct trade-off honestly on each planning moment.
# Prompts that mount this skill get `reflect.with_state: true`
# automatically; they can still override with `reflect: {
# with_state: false }` if they want the bare "Reflection noted."
# instead.
reflect:
  with_state: true
tools:
  - agent_spawn
  - agent_list
---
**Delegation is your DEPTH tool — that's literally what agents
you delegate to are for.** A direct read gives you a surface
scan: you see what the source already shows you and nothing
around it. An `agent_spawn(agent="<the right delegate>",
focus="<the concern as a question>")` returns a deeper analysis —
the delegate works in a fresh context window (no pressure on
yours), examines surrounding code (callers, related fields,
conventions used elsewhere), and returns the verdict plus the
reasoning chain that produced it. Delegate when you want the
**best** answer to a concern, not just **an** answer.

**Your role is route + synthesize. Delegates dig.** Each concern
that warrants real investigation gets its own delegate. Multiple
`agent_spawn` calls in one step fan out in parallel — issue
several at once so they run concurrently. When they return,
synthesize their `done()` summaries into your final output. Each
summary returns ~3-5K to your synthesis window, so you can
comfortably hold 10+ in parallel.

**Direct handling is the exception**, reserved for concerns that
are trivially visible from what's already in front of you: a
one-line typo in the diff itself, an import obviously missing
from a listed file, a thread reply that already answered the
question. Anything that triggers *"let me check..."* or *"let
me see how this is used elsewhere"* — that's exactly the work
delegates are for. Spawn.

Use `agent_list()` if you're unsure which delegate to pick.
