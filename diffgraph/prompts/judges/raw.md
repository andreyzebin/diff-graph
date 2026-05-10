---
agent: judge.raw
mode: single
budget:
  tokens: 30000
  steps: 1
llm:
  temperature: 0.0
summary: >
  Pass-through judge — system prompt is minimal; the caller supplies
  the FULL rendered evaluation prompt via `--user-message="..."`.
  Used by the bench-side OrchestraJudge shim that takes its existing
  prompt template and delegates only the LLM call to diff-graph's
  CLI (so judge runs land in the same trace DB / OTel layer as the
  agents they grade — uniform observability via Phase-1
  instrumentation, no special judge code path).
---
