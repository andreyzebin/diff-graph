# Task: identify concerns only

No investigation, no publishing, no verdict. Just identify
concerns about the diff and exit.

Work this way:

1. Read the diff with `diff_read_file` / `diff_outline` / `diff_list_files`
   as much as you need to understand the shape of the change.
2. Form distinct concerns the way you normally would — one line
   of inquiry per risk area, scaled to diff size.
3. Call `reflect(...)` with each concern listed under
   `questions_remaining` as `{id, text}` — `text` is the concern
   phrased as an investigation question (e.g. *"Does selectFreeItem
   actually pick the cheapest item per the AC?"*). Set `confidence`
   to `low` or `medium` (you haven't investigated yet) and
   `next_action` to *"identification only — exit"*.
4. Call `done(findings=[])` and exit.

## What NOT to use this run

These tools are available capabilities, but the task this run does
NOT need them:

- `spawn_agent` — no investigation. Stop at concerns; do not
  delegate any to investigators. The task ends after `reflect()`.
- `post_comment` — no findings to publish. Concerns are not
  findings; don't try to post them as inline comments.
- `set_review_status` — no verdict. The task isn't deciding
  approval; that's a separate run.

If you accidentally call any of the above, you've gone past the
task. Stop, call `done(findings=[])`.

The questions in your `reflect()` are the test output. That's all
this run measures.
