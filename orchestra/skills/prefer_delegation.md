---
# Skill: prefer_delegation
#
# Bundles the tools an orchestrator-role agent needs to delegate
# (agent_spawn + agent_list) with the rationale for picking
# delegation OR direct handling — both can be the right move,
# the skill makes the trade-off explicit.
#
# Abstract over delegate names: the actual name is whatever
# agent_list() returns at runtime — varies per deployment. The
# skill body never names a specific delegate or domain.
#
# History: TODO §13.10c — positive-framing rewrite. Earlier
# variants framed direct handling as an "exception" or pushed
# budget pressure to force spawning; both biased the model
# rather than informed it. This version lists rational criteria
# in both directions.
description: >-
  Adds agent_spawn + agent_list to the toolset and provides the
  trade-off rationale: when delegation is rational (parallelism,
  depth, capability mismatch, …) versus when direct handling is
  rational (trivial / in-context / synthesis). The skill never
  says "always delegate" — it makes both choices first-class
  and explicit.
# Reflects under this skill carry a live budget + time +
# subagents snapshot so the agent can price the spawn-vs-direct
# trade-off honestly at each planning moment. Prompts that mount
# this skill get `reflect.with_state: true` automatically; they
# can still override with `reflect: { with_state: false }` for the
# bare "Reflection noted." behaviour.
reflect:
  with_state: true
tools:
  - agent_spawn
  - agent_list
---
**Delegation, in one line.** `agent_spawn(agent="<name>",
focus="<the task as a question>")` runs another agent in a fresh
context window with its own budget and tool surface; it returns
a short summary to you when it finishes. Your role with this
skill is to **route** work to delegates when delegation is
rational, and **synthesize** their outputs into your final
answer.

**Use `agent_list()` first** if you don't already know which
delegates are available and what each is good at — `agent_spawn`
needs a name.

## When delegation is rational

Any **one** of these is enough — you don't need all of them to
hold:

1. **Parallel independent subtasks.** N items with no data
   dependency between them. Issue several `agent_spawn` calls
   in one step → they fan out concurrently → you wait once
   instead of N times. This is the most efficient form when it
   applies.

2. **Context-window pressure.** The subtask requires reading
   or exploring material that would crowd your own window. A
   delegate works in a fresh window; you preserve yours for
   the final synthesis step.

3. **Depth requirement.** The subtask needs recursive
   exploration (A → references-of-A → things-in-those). A
   delegate goes narrow-and-deep on that branch while you
   stay broad over the whole task.

4. **Capability mismatch.** The delegate has tools or
   methodology you don't (visible in `agent_list` output).
   Delegating gets those applied without changing your own
   tool surface.

5. **Bias / framing isolation.** The subtask should be
   approached without your current hypothesis. A delegate
   re-reads from scratch and may notice what your framing
   filtered out.

6. **Budget separation.** The subtask is expensive (many tool
   calls, lots of reading) and you want to reserve your token
   budget for the synthesis step. Delegates run against their
   own budget.

7. **Auditability.** A delegated subtask leaves an explicit
   `focus` + `done()` summary pair you (and a downstream
   reader) can inspect. Inline reasoning of the same scope is
   harder to follow afterward.

## When direct handling is rational

Also legitimate, not a fallback:

- The subtask is **one or two deterministic tool calls** with
  obvious arguments and no exploration around them.
- The information you need is **already in your current
  context** — re-reading it via a delegate just duplicates
  tokens.
- The step **is** the synthesis — only you have the full
  picture of how parts compose, so no delegate could do better.
- **Latency-critical small ops** where a spawn + wait would
  cost more than the work saved.

## Fan-out form

When the task is N independent items, the parallel form is:

    agent_spawn(agent="<X>", focus="<item 1>")
    agent_spawn(agent="<X>", focus="<item 2>")
    …
    agent_spawn(agent="<X>", focus="<item N>")

— all in a single step. Each call returns its own summary
(~3-5 K tokens) when the delegate finishes. You receive them
together and synthesize. You can comfortably hold 10+ summaries
in your synthesis window.

The choice between delegation and direct handling is per-
subtask, not all-or-nothing. A complex task often has both
shapes inside it: delegate the deep / parallel / context-heavy
parts, handle the trivial / in-context / synthesis parts
yourself.
