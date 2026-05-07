# Hardcoded user-side prompts and notification strings

Inventory of every `role: "user"` message and framework-emitted
notification text the orchestra/diffgraph code injects into the
LLM conversation outside the agent's `.user.md` template. Useful
for audit, refactoring, and translation passes.

Path / line columns refer to `feature/agent-isolation` HEAD.

## 1. Default budget pushers

Auto-attached to every compiled agent that doesn't define its own
pushers. Fires at fractions of the agent's tokens/steps budget.

### NUDGE @ 75% budget
`orchestra/compiler.py:491-492`

```text
75% budget used. Wrap up current investigation and call done().
```

Sent as a `role: user` message during the agent's ReAct loop.

### FORCE_DONE @ 100% budget
`orchestra/compiler.py:493`

No injected text. The framework restricts the available tool list
to `done()` only — the agent has to call it to exit.

### "Step limit reached" force-done message
`orchestra/agent.py:967-969`

```text
Step limit reached. Call done() now with all findings you have so far.
```

Inserted as a `role: user` message in `_force_done()` right before
the budget-capped final LLM call where only `done` is available.

## 2. require_tool guards

Defined per agent in the agent's frontmatter (`guards:` field). When
the agent stops without calling a required tool, the guard message
is injected as a `role: user` and the agent gets one more chance.

### Dispatcher's `require_tool:post_comment` guard
`diffgraph/prompts/dispatcher.md` (frontmatter)

```text
You stopped without replying. The user can only see post_comment()
output. Call post_comment(text=..., parent_id={comment_id}) once,
then finish with done().
```

The `{comment_id}` is interpolated from the agent's data scope before
injection.

## 3. Handoff messages (parent → child agent)

When a parent agent spawns a child via `spawn_agent(..., context_handoff=...)`,
the framework injects a context message into the child's history.
Wrappers in `orchestra/handoff.py`.

### `findings_only` (default)
`orchestra/handoff.py:61-62`

```text
[Previous agent's output]
<json-serialised done() output>
```

### `sgr_outcomes`
`orchestra/handoff.py:42-43`

```text
[Previous agent's final reflection]
<json-serialised last reflect() entry>
```

### `all_sgr`
`orchestra/handoff.py:52-53`

```text
[Previous agent's reasoning trajectory]
<json-serialised SGR entries>
```

### `findings_and_sgr`
`orchestra/handoff.py:71-76`

```text
[Reasoning trajectory]
<json>

[Output]
<json>
```

### `condensed`
`orchestra/handoff.py:113`

```text
[Condensed context from previous agent]
<llm-summary>
```

The summary itself is produced by an LLM call with this system
prompt (default, overridable via `CondensedHandoff(condense_prompt=...)`):

```text
Summarize the investigation so far in <500 words.
```

### `last_N` / SGR prefix
`orchestra/handoff.py:135-138`

```text
[SGR context]
<json>
```

### `compose_handoff` directives
`orchestra/handoff.py:198-216`

Per included directive:

```text
[Output]
<json>

[Last reflection]
<json>

[All reflections]
<json>

[First reflection]
<json>
```

## 4. Condensation (drop-old + summarise)

When a long conversation is compressed mid-run.

### LLM summary system prompt (default)
`orchestra/types.py:65`

```text
Summarize the conversation so far in <500 words.
```

### Condensed-history insertion
`orchestra/condensation.py:92`

```text
[Condensed history]
<llm-summary>
```

### Sliding-window placeholder
`orchestra/condensation.py:115-118`

```text
[<N> earlier messages condensed (<S> steps)]
Latest SGR: <truncated learned-string>
```

## 5. Merge of parallel branches

When two or more parallel agent branches return findings and the
framework consolidates them via LLM.

### System message
`orchestra/merge.py:108`

```text
You merge code review findings. Output JSON only.
```

### User message
`orchestra/merge.py:97-101`

```text
Multiple agent branches investigated the same codebase in parallel.
Merge their findings: deduplicate, resolve conflicts (prefer higher
confidence), and produce a single consolidated list.

Branch 1 (confidence: <c1>):
<json>
---
Branch 2 (confidence: <c2>):
<json>
…
```

## 6. Compiler — LLM metadata extraction

Used only when a prompt file lacks formal frontmatter and an LLM
client is provided as a fallback.

### System
`orchestra/compiler.py:535-540`

```text
Extract metadata from this agent prompt. Return JSON only:
{"summary": "1-3 sentence description",
 "capabilities": "comma-separated: sgr, spawn, plan, fork, etc.",
 "tools": "comma-separated tool names used",
 "mode": "single or react"}
```

### User
`orchestra/compiler.py:542`

The first 3000 chars of the prompt source.

## 7. "Begin." filler

When neither a user-message override nor `<name>.user.md` produces
text, the framework still appends a `role: user` because some
endpoints reject system-only conversations.

`orchestra/agent.py:885`

```text
Begin.
```

## 8. Injected adjustments (parent → running child)

A parent can inject ad-hoc messages into a child's queue. They land
as `role: user` in the child's next LLM round. Content is whatever
the parent passed via `Agent._injected_messages` (no fixed string).

`orchestra/agent.py:902`

## 9. Custom pusher action

`PusherType.CUSTOM` runs a user-provided handler that mutates the
messages list directly. Whatever the handler appends is the prompt.
No fixed string in the framework.

`orchestra/agent.py:941-945`

---

## Cross-cutting notes

- Most strings are **English**; condensation summaries inherit
  whatever language the LLM picks. The dispatcher guard uses an
  interpolated example with `{comment_id}` — keep that placeholder
  syntax stable when localising.
- The `Begin.` filler is the only string that reaches the LLM
  unmodified for production agent runs that don't use a
  `.user.md`. Reviewer / investigator / dispatcher all have a
  `.user.md` so they never see it. Bench unit tests that use
  `--user-message-from` override this entirely.
- Pusher messages and the force-done message are designed to be
  interruptive — terse, imperative, no negotiation. Do not
  rephrase as polite suggestions; the agent ignores them when
  hedged.
- Handoff text is **structural** — the JSON-around-bracketed-tag
  shape is what the child parses. Don't add prose without
  matching the parser.
