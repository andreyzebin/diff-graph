---
agent: judge.default
mode: single
budget:
  tokens: 30000
  steps: 1
llm:
  temperature: 0.0
data:
  assert_via:
    type: string
    description: "Channels to match required_comments against. Comma-separated subset of {pr_comments, intended_findings, intended_concerns}. Empty = pr_comments default."
  agent_comments:
    type: string
    description: "Full text of all comments the agent posted on the PR (channel: pr_comments)."
  intended_findings:
    type: string
    description: "Findings the agent emitted via done(findings=[...]) — the intended_findings channel."
  intended_concerns:
    type: string
    description: "Concerns the agent reflected on (reflect(concerns=[...])) plus spawn_agent(focus=...) strings — the intended_concerns channel."
  acknowledgement_required:
    type: string
    description: "yes if the agent was invoked via a PR comment and is expected to ack quickly; no otherwise."
  pr_diff:
    type: string
    description: "The PR diff under review."
  agents_md:
    type: string
    description: "Project AGENTS.md conventions."
  required_comments:
    type: string
    description: "JSON of expected required comments to match."
  forbidden_comments:
    type: string
    description: "JSON of forbidden topics."
  concern_focuses:
    type: string
    description: "Expected concern_focuses for LOOK-phase tests."
  expected_status_change:
    type: string
    description: "Expected PR status change (APPROVED / NEEDS_WORK / unchanged)."
  actual_status_change:
    type: string
    description: "Actual PR status change observed."
summary: >
  Default code-review judge. Evaluates whether an agent under test
  found the required comments, avoided false positives, set the
  right PR status, and reasoned about the codebase soundly. One-shot
  LLM call returning structured JSON {overall_score,
  required_comments[], false_positives[], status_change_verdict,
  agent_warnings[], summary}. Used by the QA bench scoring layer.
---
