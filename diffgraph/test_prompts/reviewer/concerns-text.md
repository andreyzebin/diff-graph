---
# `tool_choice=required` providers (deepseek, …) won't let the LLM
# produce a tool-less text turn. Wrap the deliverable in a single
# capture-style tool — agent always emits A tool call, the framework
# records the text as the run's output, judge reads it back via
# `assert_via: [intended_text]`.
tools:
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

# Interface contract for this test prompt — same Bitbucket-PR shape
# as production reviewer.user.md.
data:
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"
  commits:
    type: string
---
PR: {{ pr.title }}
{{ pr.description }}

Commits *(oldest → newest)*:

{{ pr.commits }}

Identify the concerns this diff raises and submit them via
text_answer(text=...). Use reflect during your work as private
working memory — facts learned, hypotheses still open, what's
resolved — but the run's deliverable is the text you pass to
text_answer at the end. Then call done(findings=[]).

text_answer payload shape: plain text, one concern per line as
`- <short title>: <one-sentence question>`. No preface, no summary,
no fix suggestions — just the concerns list.
