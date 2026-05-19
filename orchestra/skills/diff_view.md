---
# Skill: diff_view
#
# Bundles the four `diff_*` tools with the methodology that
# explains how the unified-diff view works — the `ref` parameter
# forms, the three-coordinate line numbering (L / old / new), and
# which coordinate to use when posting findings. Both reviewer and
# investigator need this; rather than copy the same section into
# two system.md prompts, mount the skill at the agent level and
# the body renders automatically alongside the tools.
#
# When an agent gets only a subset (`diff_list_files` for
# discovery without the read tools, say), it's still expected to
# mount this skill — the methodology applies whenever you touch
# the diff view at all. Subsetting at the prompt level via
# `tools:` (declared in the user message) is the right knob, not
# splitting the skill.
description: >-
  The unified-diff view that `diff_list_files`, `diff_read_file`,
  `diff_outline`, and `diff_search` all share. Explains the
  three `ref=` forms (`base...source`, `<sha>...<sha>`, `source`),
  the L / old / new line-number coordinates each annotated line
  carries, and which coordinate to anchor findings on
  (`new` — that's what Bitbucket pins comments to).
tools:
  - diff_list_files
  - diff_read_file
  - diff_outline
  - diff_search
---
## Diff view (how the file tools work)

`diff_list_files`, `diff_read_file`, `diff_outline`, and `diff_search`
all operate on a **unified-diff view** of the repo, controlled by
the `ref` parameter:

- `ref="base...source"` (default in PR mode) — virtual filesystem
  where each line of a changed file is annotated. Three-dot
  semantics: the diff is anchored at `merge-base(base, source)`,
  so you see only what THIS branch added — what Bitbucket's PR
  view shows, not whatever base may have advanced to.
  - `+` added in source, `-` removed from base, ` ` unchanged context.
- `ref="<sha1>...<sha2>"` — same shape, between specific commits.
- `ref="source"` — plain working-tree files, no markers.

Each annotated line carries three coordinates:

- **L** — position in the unified-diff view itself. Use for
  `start_line` / `end_line` in `diff_read_file`, and as shown in
  `diff_outline` symbol ranges.
- **old** — line number in the base commit (present on `-` and
  ` ` lines).
- **new** — line number in the source commit (present on `+` and
  ` ` lines). **Use `new` when posting findings** — that's what
  Bitbucket anchors on.

For unchanged files, or when `ref="source"`, the three collapse:
L == old == new.

### Cross-source reads (`repo=` param)

All four tools accept a `repo=` parameter that defaults to
`"default"` (the current PR's repo). Pass an explicit URI to read
from a DIFFERENT repo — typically a lib / shared codebase you
need to cross-reference. Discover the URI via `repo_list()` or
`jira_dev_info(<ticket>)`; pass the leaf URI
(`bitbucket://<handle>/<project>/<repo>`) — server / project
levels are valid for `repo_list` and `pr_list`, but `diff_*`
needs a specific repo to materialize against.
