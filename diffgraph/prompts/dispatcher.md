---
agent: dispatcher
mode: react
tools: [post_comment, react_to_comment, spawn_agent, done]
budget:
  tokens: 30000
  steps: 10
llm:
  temperature: 0.3
guards:
  require_tool:post_comment: "You stopped without replying. The user can only see post_comment() output. Call post_comment(text=..., parent_id={comment_id}) once, then finish with done()."
data:
  message:
    type: string
    description: "the full user message (may contain /command, or just plain text). Empty string when invoked outside a PR comment."
  comment_id:
    type: integer
    description: "ID of the invoking comment. 0 means no comment context (CLI / auto-trigger / benchmark)."
  comment_thread:
    type: string
    description: "full thread from root to invoking comment, or '(no thread)' when comment_id is 0."
  pr_title:
    type: string
    description: "PR title"
  pr_description:
    type: string
    description: "PR description"
  generation:
    type: string
    description: "current prompt generation name"
  mutation:
    type: string
    description: "prompt content hash (short)"
  existing_comments:
    type: string
    from: pr_context.existing_comments
summary: >
  Entry point for all user interactions. Three supported commands:
  /review (spawns reviewer), /ask (answers from PR context), /help.
  Plain text without a /command is treated as /ask.
---
