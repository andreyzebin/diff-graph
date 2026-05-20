---
agent: judge.raw
mode: react
tools:
  - agent_inspect
  - answer
budget:
  tokens: 60000
  steps: 8
llm:
  temperature: 0.0
summary: >
  Code-review judge. Runs on the orchestra engine like any other
  agent: a minimal system prompt (raw.system.md — "grade per the
  instructions, finish with answer()") plus all the grading logic
  in raw.user.md (rendered as Jinja against the bench-supplied data
  channels). The judge does NOT receive a pre-flattened tool trace —
  it is handed the run_id of the agent under review and pulls the
  behavioural evidence itself via agent_inspect(run_id=…,
  view="trace"), which routes through the AgentsRuntime provider
  (TraceDBObserver over the shared trace store in prod,
  FakeAgentsRuntime in unit tests). It submits the JSON verdict with
  answer(text=…). The bench (`benchmarks.runner.judge.LLMJudge`)
  reads the SAME raw.user.md, renders it in-process with the data it
  collected (PR diff, intended_findings, concern_focuses, the agent
  run_id…), and passes the rendered text via `--user-message` — one
  template file across both call sites, no drift.
---
