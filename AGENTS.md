# Agent Guide

This document describes the codebase for AI agents and coding assistants.

## What this project does

DiffGraph is a multi-agent PR code reviewer built on the Orchestra framework. It takes a raw `git diff` (or fetches one from Bitbucket Server), runs a three-phase review pipeline via prompt-defined agents, and produces structured `ReviewFinding` objects -- optionally posted as inline PR comments.

**Agents (defined by `<name>.system.md` + `<name>.user.md` file pairs — see [README → Prompt architecture](README.md#prompt-architecture--layered-extension-friendly)):**
- **Dispatcher** (react) — entry point for comment-triggered interactions (`/review`, `/ask`, `/help`, unknown commands). Routes to the right downstream agent or replies inline. Sees existing PR comments and the trigger comment thread.
- **Reviewer** (react with SGR + spawn_many) — three-phase review lead: analyze the diff, form concerns scaled to diff size (1–2 small, 2–3 medium, 3–5 large), spawn investigators in one round, merge their findings, set the PR review status, and call done.
- **Investigator** (react with SGR) — focused worker. Gets one concern, investigates first (read_file, read_outline, search), reflects with only genuinely unknown questions, returns findings with evidence. No spawning, no PR interaction.

No pre-indexing, no database, no persistent state. One `run_review()` call per diff. The orchestrator is ~35 lines of logic — all methodology lives in the prompt files.

## Repository layout

```
orchestra/                       Prompt-defined agent framework (~3,700 LOC)
+-- compiler.py                  Compiler: <name>.system.md + <name>.user.md -> agent registry
+-- trace.py                     Trace data collection + template preparation
+-- trace_db.py                  SQLite trace storage + reader
tracing/                         Trace CLI + web server
    +-- server/                  FastAPI trace viewer (Alpine.js + Jinja2)
    +-- app.py                   Routes, data API, WebSocket live updates
    +-- templates/               Jinja2: trace.html, macros.html, runs.html, live.html
    +-- static/                  trace.css
+-- types.py                     AgentConfig, BudgetConfig, LLMParamsConfig
+-- config.py                    YAML loading, env var expansion, validation
+-- events.py                    EventBus with typed events
+-- agent.py                     Agent: single + react, all meta-tools built-in
+-- budget.py                    BudgetState with cumulative_paid
+-- sgr.py                       SGR with question IDs + fuzzy matching
+-- handoff.py                   7 context handoff modes
+-- condensation.py              4 message condensation strategies
+-- streaming.py                 LLM streaming with param passthrough
+-- feedback.py                  Read-only behavioral signals
+-- merge.py                     Merge strategies (union, best_confidence, llm_merge, raw)
+-- prompts.py                   Template loading + regex interpolation
+-- tools/
    +-- registry.py              @register decorator, schema generation
    +-- builtin.py               Meta-tool schemas (spawn, adjust, observe, etc.)
    +-- shared.py                AppendLog, MutexMap, Blackboard

diffgraph/                       Code review domain
+-- api.py                       DiffGraph public API class
+-- orchestrator.py              One agent entry point (~35 lines of logic)
+-- orchestra_tools.py           Domain tools as closures
+-- diff_parser.py               git diff -> DiffResult
+-- lang.py                      Language detection + file extension map
+-- tools.py                     Filesystem primitives (list_files, read_file, search_text)
+-- outline.py                   tree-sitter structural outline
+-- bitbucket.py                 Bitbucket Server integration
+-- prompts/                     Two files per agent: system + user (frontmatter + body)
    +-- dispatcher.{system,user}.md   Comment-triggered routing (/review, /ask, /help)
    +-- reviewer.{system,user}.md     Three-phase review lead (analyze -> investigate -> judge)
    +-- investigator.{system,user}.md Focused investigator with SGR
+-- test_prompts/                Sibling test-only user layers (same system base)

tests/
+-- test_diff_parser.py

cli.py                           Typer CLI: run / trace / serve
config.yaml                      Committed defaults with ${VAR} placeholders
config.local.yaml                Local overrides (gitignored)
.env.example                     Environment variable template
```

## Data flow

```
parse_diff(diff_text)
  +-> DiffResult
        +- changed_files         -> file paths with status
        +- files[path].status    -> "added"/"modified"/"deleted"/"renamed"
        +- files[path].after_changed_lines  -> set of + line numbers

run_review(diff_text, repo_path, llm, model, existing_comments?)
  +-> compile_prompts()          -> agent registry from .system.md + .user.md pairs
  +-> register_diffgraph_tools() -> domain tools as closures over context
  +-> build lead config    -> inject diff_summary, existing_comments
  +-> Agent(reviewer).run()
        |
        Reviewer (react):
          Phase 1: ANALYZE -- read diff, form concerns scaled to diff size
          Phase 2: INVESTIGATE -- spawn_many("investigator", focus=concern)
              +-> Investigator (react + SGR):
                    ReAct loop: find_files, read_file, read_outline,
                    search, reflect(), done(findings)
          Phase 3: JUDGE -- merge investigator findings (dedup, not select),
                            set_review_status, done(findings)
        |
  +-> parse findings -> list[ReviewFinding]
  +-> ReviewContext (comment_replies, comment_resolves)
```

Data inheritance: parent's `data_scope` is auto-injected into child `{placeholders}`. No handoff context by default -- child gets everything via its system prompt.

## Key abstractions

### `DiffResult` (`diff_parser.py`)

- `changed_files` -- after-paths (excludes deleted)
- `files[path].status` -- `"added"` / `"modified"` / `"deleted"` / `"renamed"`
- `files[path].after_changed_lines` -- set of 1-indexed `+` line numbers
- `files[path].hunks` -- list of `HunkSnippet` with `before_lines` / `after_lines`

### `ReviewFinding` (`orchestrator.py`)

Output of the review. Fields:
- `file` -- relative path
- `line` -- most relevant line number in the changed code
- `severity` -- `"BLOCKER"` / `"MAJOR"` / `"MINOR"` / `"COMMENT"`
- `title` -- one-line summary
- `explanation` -- what the problem is and why it matters
- `evidence` -- code evidence supporting the finding
- `suggestion` -- optional concrete fix

### `ReviewContext` (`orchestrator.py`)

Side-effectful actions collected during the review:
- `comment_replies` — `[{comment_id, text}]` to POST after the run
- `comment_resolves` — `[comment_id]` to mark resolved after the run
- `review_status` — `Optional[str]`: `"APPROVED"` / `"NEEDS_WORK"` / `"UNAPPROVED"` or `None` (don't touch). Set via `set_review_status` tool.
- `review_status_reason` — short text recorded for audit alongside the status.

### `set_review_status` tool (`orchestra_tools.py`)

The reviewer's verdict on the PR as a whole. Default policy lives in
`reviewer.system.md`: BLOCKER/MAJOR finding stands → `NEEDS_WORK`; only
MINOR/COMMENT or no findings → `APPROVED`; honestly unable to judge
→ leave unset. Strictness is described in prose, not hardcoded
numbers — the prompt explicitly notes that the rule adjusts to the
situation (chore vs critical-path feature).

The tool is a no-op in production unless `--bot-user` is configured —
without an explicit account, we never alter the PR's reviewer status.

### Output interface contract

Every comment the agent posts must:

- start with the bot's account tag in square brackets (e.g.
  `[tuz_spasibo__qodo]`) when `--subject-pattern` + `--bot-user` are set.
  Lets analytics, judges, and follow-up rounds tell agent comments from
  human ones even when everything posts under the same Bitbucket token.
- end with the dg traceability footer in inline code:
  `dg:diffgraph:<prompt_hash>:<run_id>`. Built by
  `comment_meta.build_comment_meta` and applied via the `decorate`
  callback in `_publish_to_pr`. Lets pr-analytics group acceptance /
  feedback rates per prompt generation.

Stamping is done at the publish layer, not in the prompt: agents write
plain text, `_publish_to_pr` adds the prefix (when configured) and the
footer (always).

### Agent (`orchestra/agent.py`)

Two modes:
- **single** -- one LLM call, no tools
- **react** -- non-deterministic tool loop with SGR

Meta-tools built in (registered when listed in the agent's effective tool surface, NOT all auto-registered): `agent_spawn`, `spawn_many`, `plan`, `fork`, `adjust_agent`, `observe_agents`, `agent_list`, `reflect`, `done`, `answer`. `answer(text=...)` is a terminal capture tool — captures text as the run's deliverable AND ends the agent loop in a single call (the single-step closer for abstract-reasoning fixtures and text-deliverable flows; investigator / reviewer use `done(findings=[...])`, fixtures use `answer`).

### Skills — composable bundles of tools + methodology (`orchestra/skills/`)

A skill is a `.md` file with YAML frontmatter (`tools:` it brings, optional `reflect: {…}` / `extra_tools` config) + a body that the framework appends to the agent's system message (separated by `---`). Mounted via `skills:` in the system.md frontmatter (always-on for that agent) or the per-call user prompt (per-task addition). The two lists union with dedup at `Agent.__init__`.

Current set: `reflect`, `prefer_delegation`, `diff_view`, `pr_threads`, `project_conventions`, `finding_format`. See README §"Skills" for the table of what each bundles and where it's mounted.

Why split rather than inline in system.md: a skill is a single source of truth shared across agents. Updating the diff-view methodology happens in one file, not in every system prompt that documents the four `diff_*` tools.

### SGR with question IDs (`orchestra/sgr.py`)

Structured self-reflection. Each question gets a stable ID. Fuzzy matching links questions across reflect() calls even when wording drifts. Fields: `learned`, `questions_remaining`, `resolved_questions`, `confidence`, `next_action`. The `reflect` tool itself lives on the `reflect` skill — agents opt in by mounting it.

### Budget (`orchestra/budget.py`)

Tracks cumulative paid (sum of per-step deltas) with cache discount. Agents use their own per-prompt budget (`budget:` frontmatter). Pusher pipeline runs every step — see `orchestra/budget.py` (RatioPusher, TimeBudgetPusher, ReflectCadencePusher) and the REQUIREMENTS.md pusher-pipeline section.

### Trace system (`orchestra/trace_db.py`, `orchestra/trace.py`, `tracing/`)

SQLite DB persists events per-step (crash-safe). FastAPI trace server with Alpine.js frontend. Two views:

**Navigator** (`/runs/{id}/trace`):
- Split-pane: agent tree left, detail tabs right (draggable divider)
- `[⧉]` buttons load full data on demand from API (messages, tool calls, results)
- Right panel toolbar: `📋 Copy` to clipboard, `{ } JSON` toggle
- Tool call args pretty-printed; message content shown as plain text
- Token usage per step: `↑` new input, `↓` output, `©` cached
- Agent header shows totals: `↑total_in ↓total_out ©total_cached`

**Live** (`/runs/{id}/live`):
- Real-time event stream via WebSocket
- Bulk-loads existing events on open, then streams new ones
- Child agents color-coded with `[reviewer:Focus]` tags
- Tool args preview in event lines
- Auto-scroll pauses when user scrolls up

Runs list (`/`) auto-refreshes every 3s. Both views link to each other.

### `get_outline` (`outline.py`)

Tree-sitter structural outline of a source file. Returns plain text:
```
# src/Foo.java  (120 lines)
[class] FooService  L10-118
  [method] process  L15-40 *
  [method] validate  L42-60
```
`*` marks symbols overlapping `changed_lines`. Session-cached for unchanged files. Falls back to a line-count header when tree-sitter is unavailable.

### `bitbucket.py`

`fetch_pr(pr_url)`:
1. REST API for PR metadata (title, description, fromRef/toRef SHAs)
2. `git clone --filter=blob:none --single-branch` of the source branch
3. Auth baked into repo config for lazy blob fetches
4. `git fetch --filter=blob:none origin <toRef_sha>` for merge-base
5. `git diff toRef...fromRef` (three-dot = merge-base diff matching PR UI). The diff-VFS in `diffsearch/` (driving `diff_list_files` / `diff_read_file` / `diff_search` / `diff_outline`) uses the same three-dot scope — see [`diffsearch/README.md`](diffsearch/README.md) for why.

`post_review_comments(pr_url, comments, changed_lines?)`:
- Snaps each comment's line to nearest changed line for valid Bitbucket anchor
- Falls back to general PR comment if file has no changed lines
- Severity mapping: `BLOCKER`/`MAJOR` -> `BLOCKER`, `MINOR`/`COMMENT` -> `NORMAL`

`get_pr_comments`, `reply_to_pr_comment`, `resolve_pr_comment` -- thread interaction.

## Common tasks

### Add a language

1. `lang.py`: add to `LANG_MAP` and `FILE_EXTENSIONS`
2. `outline.py`: add to `_TS_LANG`, `_CONTAINERS`, `_MEMBERS`

### Add a new agent

1. Create `diffgraph/prompts/<name>.system.md` (frontmatter + methodology) and `<name>.user.md` (per-call task template) — see [README → Prompt architecture](README.md#prompt-architecture--layered-extension-friendly) for the field shape
2. The LLM compiler auto-discovers it -- no code changes needed
3. Other agents can find it via `agent_list` and spawn it via `agent_spawn`

### Add a new domain tool

1. Add the tool function in `diffgraph/orchestra_tools.py` using `@registry.register`
2. Reference the tool name in the agent's `tools:` frontmatter (base toolkit) or in a user-layer `tools_add:` (per-task extension)
3. The tool is a closure over the review context (`_Ctx`)

### Change review methodology

Edit the prompt files in `diffgraph/prompts/` (system layer = methodology; user layer = per-call task):
- `dispatcher.{system,user}.md` — comment-triggered routing, when to /review vs /ask vs unknown-command, context-focus rules (deep thread → focus on thread; shallow → primary context is whole PR)
- `reviewer.{system,user}.md` — three-phase methodology, concern scaling, severity guide, set_review_status policy
- `investigator.{system,user}.md` — investigation workflow, evidence requirements, when to call done

All methodology lives in prompts, not in Python code. The orchestrator is ~35 lines.

### Prompts-as-methodology principle

Prompts describe **how to think and collaborate** in code review, the way
an experienced reviewer would explain it to a junior. They do not assert
fixed numeric thresholds, MUST/SHALL contracts, or counts the model has to
hit. Examples of what *not* to write:

> Bad: "Drop a finding ONLY if it duplicates another OR has no evidence.
>      MUST forward all BLOCKERs. Sanity check: 12 → 1 is a synthesis bug."
>
> Good: "Investigators already filter for noise — your role at this stage
>      is to weave their sets together, not to re-judge each finding. If
>      your merged set ends up much smaller than what came in, sit with
>      that — usually it means a real finding got dropped, not that there
>      was that much true overlap."

Reasons:
- LLMs interpret rigid directives as adversarial puzzles to satisfy
  literally. Methodology in prose lets the model exercise judgement.
- Hardcoded numbers age badly — "12 distinct findings → 1 is a bug"
  cues the model to count, not to reason about content.
- Different PR shapes need different defaults; rigid prompts can't
  adapt to chore vs feature vs hotfix.

When you must convey a default, make it a default-with-room: "default
policy: <X>; adjust strictness to the situation". Concrete consequences
of this principle are visible in the CONSOLIDATE step (reviewer.system.md)
and the SET REVIEW STATUS step.

### Investigators filter, reviewer merges

Each investigator returns findings already filtered for evidence on
their end. The reviewer's CONSOLIDATE step is **deduplication, not
selection** — same file + same line + same problem merges; everything
else carries forward. A reviewer that drops half the investigators'
findings without duplicates is a synthesis bug, not concision.

### Change agent behavior at runtime

Parent agents can modify children via `adjust_agent`:
- Change temperature, penalties, model
- Inject a message into the child's conversation
- Extend the child's step budget

### Add a new provider

Create `diffgraph/<provider>.py` following the pattern in `bitbucket.py`:
`fetch_pr()` returns `(diff_text, repo_path, cleanup_fn, pr_meta)`.

### View a run

```bash
python cli.py trace --log        # console trace: call/result per step, agent tree
python cli.py trace              # open last run in browser (starts trace server)
python cli.py trace --list       # recent runs table
python cli.py trace --run ID     # specific run
```

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Covers `diff_parser` without an LLM. To test the full pipeline, point at a real LLM endpoint and run `cli.py run` against a local diff.
