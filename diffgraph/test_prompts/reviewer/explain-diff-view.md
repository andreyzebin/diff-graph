---
# Diagnostic prompt — NOT a code review. Asks the agent to introspect
# its own mental model of the diff-view tools. Used to confirm whether
# §3.4 in TODO.md (markers-as-content confusion) is a real failure
# mode for a given provider.
tools:
  - text_answer
extra_tools:
  - name: text_answer
    description: "Submit your introspective answer as plain text. Call once at the end."
    parameters:
      type: object
      properties:
        text:
          type: string
          description: "Structured answer to the introspection questions, plain text."
      required:
        - text

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

This is an **introspection task** — do NOT review the PR.

1. Call `diff_list_files()` once with no arguments to see the
   current output format.

2. Then submit via `text_answer(text=...)` a structured description
   with these sections (use the literal letters as headers):

   a) **Columns** — list every column you see in each row, in order,
      and describe what you understand it to encode.

   b) **Leading marker (M / A / D / blank)** — what does each
      character mean to you? If a row starts with blank, is that file
      part of the diff or not?

   c) **`+N/-N` inside the size parentheses** — what does this
      encode? If I asked you to find LINES that were added in this
      PR, what tool call would you make? Would `diff_search(query="^+")`
      work? Why or why not?

   d) **Focus on substantive changes** — if I asked you to focus only
      on files with substantive content changes, which entries from
      the list would you pick, and how would you decide? Be specific
      about your *next* tool call and the exact arguments you'd pass.

3. Call `done(findings=[])` to finish.

Be honest. The point is to surface any mental-model mismatch, not to
produce a "correct" answer. If you're unsure, say so explicitly in
the relevant section rather than guess.
