---
# Budget-aware delegation variant. The reviewer sees its run state
# (budget + time + subagents) on EVERY reflect call — opted into via
# `reflect_response_template: with_state`. No separate budget_stats
# query needed; the snapshot arrives in the reflect tool-result, so
# the reviewer plans on fresh data every reflection cycle.
#
# Delegation rationale is DEPTH-AS-UPGRADE positive framing
# (see §13.10c iteration).
#
#   Positive frame: "investigators are your DEPTH tool — that's
#   literally their job. A direct read gives you a surface scan.
#   A spawn returns a deeper analysis: the child reads in a fresh
#   window, examines surrounding code, returns verdict +
#   reasoning. Use investigators when you want the BEST answer."
#
#   Reviewer's role: route + synthesize. Investigators dig.
#   Direct handling is the EXCEPTION (trivial concerns visible
#   in the diff itself), spawn is the default for anything
#   requiring "let me check..." reasoning.
#
# Why positive framing beats the previous "do NOT use
# diff_read_file for exploration" rule:
# - Models (especially deepseek-chat) ignore prohibitions more
#   readily than they ignore quality upgrades. "Spawn for deeper
#   analysis" is something the model WANTS to do; "don't read
#   yourself" is something it sandbags against.
# - The positive frame includes the WHY (fresh window, surrounding
#   context, parallel deep dives) so the model's own reasoning
#   chain endorses the choice rather than fighting it.
# - Coverage/breadth angle from the previous iteration is still
#   true but moved to a supporting role — depth is the lead pitch.
#
# Tight `max_context` (16K) stays as a backstop — the positive
# framing should drive spawning at any context size, but the
# tight window discourages "I'll just peek at one more file"
# drift while we calibrate.
#
# Production reviewer.user.md stays untouched until this shape
# proves itself on the bench.
reflect_response_template: with_state
budget:
  max_context: 16000
tools_add:
  - agent_spawn
  - agent_list
  - text_answer
extra_tools:
  - name: text_answer
    description: "Submit your final concerns list. Call once at the end with the full list as `text`. The agent's only deliverable channel for this task."
    parameters:
      type: object
      properties:
        text:
          type: string
          description: "Concerns list, plain text, one per line as `- <short title>: <one-sentence question>`."
      required:
        - text

# Interface contract — same Bitbucket-PR shape as concerns-text, plus
# jira_tickets so the reviewer can ground concerns in the ticket AC
# before delegating.
data:
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"
  commits:
    type: string
    from: pr_context.commits
  jira_tickets:
    type: string
    description: "Jira ticket reference(s) this PR is associated with"
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

This PR is associated with these Jira ticket(s): {jira_tickets}

**Every reflect call returns your run state.** Each `reflect(...)`
tool-result carries the current snapshot: time, your own context
window usage, the shared pool with children, any spawned subagents,
and a "typical investigator spawn" cost reference. Re-plan each
time you reflect — your situation has changed.

{budget_stats_legend}

**Read the ticket** via `jira_read_ticket(ref)` (copy each ref
verbatim from the list above), then **orient on the diff** with
`diff_list_files`.

**Investigators are your DEPTH tool — that's literally their
job.** A direct read gives you a surface scan: you see what the
diff highlights and nothing around it. An
`agent_spawn(agent="investigator", focus="<the concern as a
question>")` returns a deeper analysis — the child reads in a
fresh context window (no pressure on yours), examines surrounding
code (callers, related fields, conventions used elsewhere in the
repo), and returns the verdict plus the reasoning chain that
produced it. Use investigators when you want the **best** answer
to a concern, not just **an** answer.

**Your role is route + synthesize. Investigators dig.** Each
concern that warrants real investigation gets its own
investigator. Multiple `agent_spawn` calls in one step fan out in
parallel — issue several at once so they run concurrently. When
they return, synthesize their `done()` summaries into your final
concerns list. Each summary returns ~3-5K to your synthesis
window, so you can comfortably hold 10+ in parallel.

**Direct handling is the exception**, reserved for concerns that
are trivially visible: a one-line typo in the diff itself, an
import obviously missing from a listed file, a thread reply that
already answered the question. Anything that triggers
*"let me check..."* or *"let me see how this is used elsewhere"*
— that's exactly the work investigators are for. Spawn.

Use `agent_list()` if you're unsure which agent name to pass.

**Synthesize.** When all spawns have returned, compile the final
concerns list. Submit via `text_answer(text=...)`, one concern per
line as `- <short title>: <one-sentence question>`. Cite the ticket
AC where relevant. No preface, no summary, no fix suggestions.
Then call `done(findings=[])`.

Use `reflect` actively — both as private working memory AND as your
state-refresh checkpoint (the snapshot in its tool-result is your
authoritative view of where you are).
