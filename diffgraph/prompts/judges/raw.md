---
agent: judge.raw
mode: single
budget:
  tokens: 60000
  steps: 1
llm:
  temperature: 0.0
summary: >
  Code-review judge. A single-shot orchestra agent: one LLM call, no
  tools. The system prompt is minimal (raw.system.md — "grade per the
  instructions, JSON only"); all the grading logic lives in
  raw.user.md, rendered as Jinja against the bench-supplied data
  channels. One of those channels is `tool_trace` — the agent's
  behavioural evidence, the compact tool-call log built by the bench
  from the canonical trace store via the AgentsRuntime provider
  (TraceDBObserver.observe + compact_tool_trace) and injected into
  the prompt like any other rendered channel. The bench
  (`benchmarks.runner.judge.LLMJudge`) renders the SAME raw.user.md
  in-process and passes the result via `--user-message` — one
  template file across both call sites, no drift.
---
