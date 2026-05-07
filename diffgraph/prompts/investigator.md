---
agent: investigator
mode: react
tools: [find_files, read_file, read_outline, search, reflect, done]
budget:
  tokens: 15000
  steps: 20
sgr_interval: 3
llm:
  temperature: 0
data:
  diff_summary:
    type: string
    from: pr_context.diff_summary
  existing_comments:
    type: string
    from: pr_context.existing_comments
  commits:
    type: string
    from: pr_context.commits
  focus:
    type: string
    description: "high-level concern to investigate (from lead)"
summary: >
  Focused code reviewer. Receives a high-level concern, investigates
  with tools, uses SGR to track reasoning, returns findings with
  evidence.
---
