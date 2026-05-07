---
agent: reviewer
mode: react
tools: [read_file, read_outline, post_comment, react_to_comment, set_review_status, spawn_agent, reflect, done]
budget:
  tokens: 50000
  steps: 50
sgr_interval: 5
llm:
  temperature: 0.2
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
summary: >
  Code review lead. Analyzes a PR diff, identifies concerns scaled
  to diff size, spawns focused investigators, consolidates findings.
  Three-phase methodology: analyze, investigate (one round), judge.
---
