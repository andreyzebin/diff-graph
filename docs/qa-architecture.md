# Quality management architecture — how we keep improving the agent

A working code-review agent isn't a one-shot deliverable, it's a
moving target: every prompt edit, every new tool, every LLM provider
update can drift behavior in invisible ways. This doc describes
how we keep that drift visible and reversible — the closed loop
that turns "did this change make the agent better?" from a feeling
into a number.

Updated: 2026-05-12.

## The keystone metric — production merge acceptance

The strongest signal we have about agent quality lives in
**pr-analytics' `merge_acceptance_rate`**: for each inline comment
the agent posts on a production PR, an LLM judge fetches the diff
the human author actually merged and decides whether the agent's
point was addressed (YES / PARTIAL / NO). The rate is

```
   merge_acceptance_rate = (YES + 0.5 × PARTIAL) / (YES + PARTIAL + NO)
```

Why it's strongest:
- it measures **outcome** — code that shipped, not opinions about
  the comment in isolation
- the judge has the full diff in hand, so "comment said X, code did Y" is decidable rather than estimated
- it's continuously collected on every production PR, so the sample
  size grows organically without bench costs

Everything below is in service of moving this number up without
regressing it. Bench scores are PROXIES — they exist to predict
merge_acceptance_rate, not replace it.

## The three loops

Three measurement loops, each running at a different cadence and
asking a different question:

```
                       ┌─ pr-analytics select-golden ──┐
                       │  (weekly, GOLD → new SCEN)    │
                       ▼                               │
   ┌────────────┐  ┌────────────────┐  ┌───────────────┴───┐
   │ UNIT tier  │→ │ INTEGRATION    │→ │ PRODUCTION         │
   │ minutes    │  │ 30-60 min      │  │ continuous         │
   │ /qa/scoring│  │ /qa/scoring    │  │ pr-analytics       │
   │ "did this  │  │ "does the full │  │ "did the merged    │
   │  agent's   │  │  pipeline      │  │  diff actually     │
   │  prompt    │  │  still work    │  │  address what the  │
   │  regress?" │  │  end-to-end?"  │  │  agent said?"      │
   └────────────┘  └────────────────┘  └────────────────────┘
       │                   │                   │
       └───── faster ──────┴───── slower ──────┘
            cheaper          ground truth
```

Signals propagate **right-to-left**: production gaps drive bench
scenarios, bench regressions block deploys, unit-tier regressions
block merges.

### Loop 1 — UNIT (per commit, per fixture)

Each fixture exercises one agent against a known-good rubric. No
spawn fan-out, no real Bitbucket. Score is judge verdict on
`reflect(...)` / `done(findings=...)` / fake-PR sink — whichever
channel that agent uses.

Lives in `code-review-benchmarks/benchmark/scenarios/unit/*`
(yaml). Runner is `bench run-unit`. Fake provider is
`diff-graph/diffgraph/bitbucket_fake.py` (class-based, isolated per
test). After the agent subprocess exits, run_unit builds a
`FakeBenchPRView` over the captured sink + payload and invokes
`LLMJudge.evaluate(scenario)` — same judge code the integration
tier uses, no parallel codebase.

**Question it answers.** "Did this prompt edit break the agent's
reasoning shape on a curated case?"

**Cadence.** Every commit (smoke), plus on-demand via /qa/plans.
~5-30s agent + 5-15s judge per fixture; n=5 batch under 5 min.

**What we DON'T trust unit-tier scores to mean.** That the agent
is good in production. That a 0.85 fixture passes a 0.80 baseline
threshold means anything beyond "this fixture didn't regress in
isolation". Unit-tier scores are FLOOR signals — a regression
means something broke, but holding the score doesn't mean the
deployed agent is good.

### Loop 2 — INTEGRATION (nightly + on merge candidate)

Same scenarios run through the full pipeline against real
Bitbucket PRs — actual post_comment / spawn_agent /
set_review_status. Catches everything the unit tier hides: parallel
spawn ordering quirks, mocked-vs-real Bitbucket API divergences,
end-to-end latency, multi-provider retry behavior.

Lives in `scenarios/agents/*` + `scenarios/java/*` +
`scenarios/interaction/*`. Runner is `bench run`.

**Question it answers.** "Does the agent still produce good output
when nothing is stubbed?"

**Cadence.** Nightly + before each release candidate merge. 30-60
min per pass. Same providers as production.

### Loop 3 — PRODUCTION (continuous via pr-analytics)

Production loop is in a separate repo
(`/home/andrey/repos/pr-analytics`) with its own SQLite cache of
Bitbucket activity. Three metrics matter for agent quality:

| Metric | What it measures | LLM judge? |
|---|---|---|
| `merge_acceptance_rate` | did the merged diff address the agent's inline comment | yes — `analyze-merges`, prompt `judge_merge_acceptance.txt` |
| `feedback_acceptance_rate` | did the next non-bot comment / commit address the agent's claim | yes — `analyze-feedback` |
| `cycle_time`, `throughput`, `acceptance_rate` | PR open→merge, PRs/period, merged% | no — pure SQL |

The first two are continuous on every merged PR; the third are
collected automatically as the cache backfills.

**Question it answers.** "When the agent leaves a comment in
production, does anything good happen because of it?"

**Cadence.** `analyze-merges` and `analyze-feedback` runs on a
weekly cron over the previous 7-14 days. Charts surface as trends
via `pa trend --metrics merge_acceptance_rate,feedback_acceptance_rate
--period biweek`.

## The improvement loop

How a prod observation becomes a bench scenario becomes a fix:

```
   1. pa trend ────────► merge_acceptance_rate dipping last 2 weeks
                              │
                              ▼
   2. pa select-golden ── classify which comments scored NO
      (drilldown)               │
                              ▼
   3. pick 2-3 PRs ──────► copy diff + human comment thread into
      where the agent          a SCEN-NNN.yaml (integration) +
      missed                   <AGENT>-U-NNN.yaml (unit mirror)
                              │
                              ▼
   4. fire new SCEN ─────► see agent baseline on these — likely
      via /qa/plans            fails or scores low (otherwise
                              why is prod missing them?)
                              │
                              ▼
   5. iterate on prompt ─► /qa/scoring trend chart shows whether
      / tool surface           each prompt edit improves the new
                              scenarios WITHOUT regressing
                              existing ones
                              │
                              ▼
   6. ship & watch ──────► next pa trend window shows whether
                              merge_acceptance_rate recovered
```

Steps 1-2 weekly (cheap, mostly SQL + cached LLM judgements).
Steps 3-4 per session when a regression surfaces. Step 5 is the
core inner loop — many prompt edits, fast bench feedback, no
production exposure. Step 6 closes the loop weeks later when prod
data accumulates.

### Why `select-golden` is the bridge

`pa select-golden` runs a five-phase pipeline over recent merged
PRs:

1. **heuristic** — fast SQL filter (lifetime, reviewer count,
   comment count) to a candidate set of ~50
2. **classify** — LLM tags each comment with type (СТИЛЬ /
   ГЛУБОКАЯ_ЛОГИКА / АРХИТЕКТУРА / БЕЗОПАСНОСТЬ / БИЗНЕС_ЛОГИКА /
   ТЕСТЫ / …) and depth
3. **analyze** — `feedback_acceptance_rate` judge on un-analyzed
   comments
4. **score** — composite per-PR score (deep-vs-surface ratio,
   acceptance, …)
5. **judge** — final verdict GOLD / SILVER / REJECT

GOLD PRs are what we mine for new bench scenarios. Two things make
them valuable:
- they're **production-real**: the diff existed in the wild, the
  human reviewer's accepted comments are ground-truth concerns
- they're **classified**: we know which axes (security / arch /
  business logic / …) are under-covered in the bench by looking
  at the GOLD distribution vs. the existing scenario tag mix

## What "stable improvement" requires

Three guarantees the architecture has to give, otherwise
improvement is one-step-forward-two-back:

### A — leak-free fixtures

If a fixture pre-tells the agent the answer, the score is
circular: prompt says "look at X", agent reflects on X, judge
scores "found X". Two static guards catch this:

- `benchmark/tests/test_unit_fixture_leak_check.py` — no
  expected_output keyword may appear in the fixture's own input
  (user_message_from / agent_data.* / pr_state metadata / trigger
  / seed comments). Per-fixture `leak_allowlist: [...]` for
  legitimately unavoidable overlaps.
- `diff-graph/tests/test_prompts_no_fixture_leak.py` — production
  agent prompts (`diffgraph/prompts/*.md` except `judges/`) can't
  contain code identifiers (CamelCase / parens / @ / ALLCAPS) that
  any bench fixture grades on. Auto-derives the forbidden list
  from the bench yamls — new fixtures auto-extend coverage.

Both run on every pytest pass. Both caught real leaks during
their first runs (the May-2026 cleanup pass — 4 leaks in
production prompts + 3 in fixtures).

### B — closed-loop feedback metric

The unit tier's score has to predict (or at least correlate with)
`merge_acceptance_rate`. We do NOT have this calibrated yet —
it's a TODO. The minimum is to plot:

```
   agent prompt SHA → unit-tier mean score (last 7 days) →
                       prod merge_acceptance_rate (matched deploy window)
```

When the two diverge, the unit tier is the problem (it's measuring
something that doesn't matter in prod) and needs new scenarios
from `select-golden`. When they agree, unit-tier scores are an
honest leading indicator and we can trust them as a deploy gate.

This calibration loop is what TURNS the bench into a stable
improvement engine; without it the bench is just regression
plumbing.

### C — observable cost

Every prompt edit changes both quality AND cost. The bench
captures both via OTel — `~/.diffgraph/traces.db` records tokens
in / out / cached + duration per agent run. The `efficiency` axis
of the five-axis scoring (TODO §5e.16) is the per-finding /
per-tool cost view; without it a "better" agent that takes 3×
more tokens looks like a wash on quality alone but is a
deployment cost regression. Currently shown on
`/qa/sessions/<run_id>` per-run; not yet aggregated as a separate
score axis.

## Daily / weekly / per-release rhythm

**Per commit (CI).**
- `pytest` in both repos — leak guards, unit-test logic checks
- TODO: smoke-fire 5-6 sentinel unit scenarios (`tier:smoke`,
  §5e.16) and fail loud if any score drops >10% from 7-day
  baseline. Today: not automated, run via /qa/plans on demand.

**Daily.**
- Integration tier nightly — full bench against real Bitbucket
- `pa fetch` to keep production data current
- Auto-fire any new scenarios added in the last 24h for an n=5
  baseline

**Weekly.**
- `pa trend --metrics merge_acceptance_rate,feedback_acceptance_rate
   --period biweek` → eyeball
- `pa select-golden` over the last 7 days → triage GOLD candidates,
  add 1-3 new scenarios
- `pa analyze-feedback` to keep the feedback judge cache warm

**Per release.**
- Full bench (unit + integration) on the release candidate commit
- Compare `/qa/scoring` against the previous release's baseline
  (the trend chart uses equal-spaced ordinal mutations so
  regressions show as level shifts between adjacent ticks, not
  smeared by attempt-count density)
- If `feedback_acceptance_rate` for the previous release dropped
  but the bench didn't catch it — that's a scenario coverage gap.
  Fire `select-golden` over the deploy window, mine the
  rejection-heavy PRs.

## Reference

- `code-review-benchmarks/README.md` — bench user guide
- `code-review-benchmarks/AGENTS.md` — bench architecture (loaders, factories, judge interface)
- `code-review-benchmarks/benchmark/runner/run_unit.py` — unit tier runner + judge wiring
- `code-review-benchmarks/benchmark/runner/fake_view.py` — fake-bitbucket view for the judge
- `diff-graph/diffgraph/bitbucket_fake.py` — class-based fake provider
- `diff-graph/tracing/README.md` — trace storage + CLI
- `diff-graph/TODO.md` §5d.3, §5e.14, §5e.16 — open work items in the QA roadmap
- `pr-analytics/README.md` — production metrics + DSL
- `pr-analytics/pa/cmd_merge_analysis.py` — merge_acceptance_rate implementation
- `pr-analytics/pa/cmd_select_golden.py` — GOLD/SILVER PR selector

## Glossary

- **Tier:unit** — one agent in isolation, fake bitbucket, fast judge. Predicts regressions.
- **Tier:integration** — full pipeline, real bitbucket. Catches end-to-end issues.
- **Tier:smoke** — TODO: a 5-6 sentinel subset of unit tier fired on every commit.
- **Tier:chaos** — TODO: resilience scenarios (rate limit, OOM, malformed input). Resilience axis.
- **GOLD PR** — production PR with high deep-comment density and high acceptance, classified as bench-worthy by `pa select-golden judge` phase.
- **Leak** — fixture input contains the keyword its expected_output grades on. Caught by `test_unit_fixture_leak_check.py` / `test_prompts_no_fixture_leak.py`.
- **assert_via** — which channel the judge reads to score: `pr_comments` (real comments via fake-PR sink), `intended_findings` (done(findings) args), `intended_concerns` (reflect(questions_remaining) args).
- **linked_run_id** — column on `runs` rows linking an agent run to its judge counterpart. Set by `LLMJudge._finish_trace()`. Required for `/qa/scoring` to surface a scored row.
