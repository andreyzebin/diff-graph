# Hardcoded user-side prompts and notification strings

Inventory of every `role: "user"` message and framework-emitted
notification text the orchestra/diffgraph code injects outside the
agent's `.user.md` template.

## Live (centralised in `orchestra/prompts/internal/`)

These actively fire in production runs and are editable as plain
Markdown alongside agent prompts.

### `pushers/nudge.md` — NUDGE @ 75% budget

```text
75% budget used. Wrap up current investigation and call done().
```

Default pusher attached by `orchestra/compiler.py:_parse_budget_header`
to every agent that doesn't define its own pushers. Sent as
`role: user` mid-run when the agent's tokens/steps usage crosses
the 75% mark.

### `pushers/step_limit.md` — Force-done at step exhaustion

```text
Step limit reached. Call done() now with all findings you have so far.
```

Injected by `Agent._force_done()` (`orchestra/agent.py`) right before
the budget-capped final LLM call where only `done` is available.

## Live but stays in agent frontmatter

### Dispatcher's `require_tool:pr_post_comment` guard

Defined in `diffgraph/prompts/dispatcher.md` (frontmatter `guards:`).
Fires when the dispatcher stops without calling `pr_post_comment`.

```text
You stopped without replying. The user can only see pr_post_comment()
output. Call pr_post_comment(text=..., parent_id={comment_id}) once,
then finish with done().
```

`{comment_id}` is interpolated from the agent's data scope.

## Inline defensive fallback

### `Begin.` filler

`orchestra/agent.py:_build_messages` adds `Begin.` as a last-resort
`role: user` when neither a user-message override nor the agent's
`user.md` produces text. **Never fires for the current three agents**
(dispatcher / reviewer / investigator all have `user.md`). Inlined
with a comment because externalising a string the LLM never reads
adds clutter for no benefit.

## Dead code paths (kept in source, not used)

The orchestra framework still has Python code for these but no
agent / config currently invokes them. Strings live in the source
because removing the code is a separate cleanup.

- **Handoff modes** (`orchestra/handoff.py`) — `findings_only`,
  `sgr_outcomes`, `all_sgr`, `findings_and_sgr`, `condensed`,
  `last_N`, `compose_handoff`. Activated only when an agent passes
  `context_handoff="..."` to `agent_spawn`. None of the current
  agent prompts do.
- **Condensation** (`orchestra/condensation.py`) — `LLMSummary`,
  `SlidingWindow`, `DropToolResults`, `Hybrid`. Gated by
  `CondensationConfig.enabled` which defaults to `False`. No agent
  enables it.
- **Parallel-branch merge** (`orchestra/merge.py`) — only invoked
  by `fork()` / `create_topology()` flows; those tools were dropped
  in the spawn_many/plan/fork cleanup commit `ce8c4eb`.
- **Compiler LLM-extract metadata fallback** (`orchestra/compiler.py:
  _llm_extract_metadata`) — used to be a fallback for prompts
  without explicit `agent:` headers. After the YAML frontmatter
  migration, every prompt has explicit headers; the LLM fallback is
  effectively unreachable.

When any of these paths gets re-enabled, externalise the strings
into `orchestra/prompts/internal/<area>/` at that time.

## Loader

```python
from orchestra.prompts import load_internal

text = load_internal("pushers/nudge")
text = load_internal("pushers/step_limit")
```

Cached after first read. Trailing newline stripped. Use
`interpolate(text, **vars)` to fill `{placeholders}`.
