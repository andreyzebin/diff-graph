---
agent: judge.raw
mode: single
budget:
  tokens: 30000
  steps: 1
llm:
  temperature: 0.0
summary: >
  Code-review judge. The system prompt is minimal ("evaluate per
  the instructions, JSON only"); all the grading logic lives in
  raw.user.md (rendered as Jinja against the bench-supplied data
  channels). The bench (`benchmarks.runner.judge.LLMJudge`) reads
  the SAME raw.user.md, renders it in-process with the data it has
  collected (PR diff, intended_findings, concern_focuses…), and
  passes the rendered text via `--user-message`. Single template
  file across both call sites — no drift.
---
