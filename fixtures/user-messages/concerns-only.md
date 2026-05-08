Task this run: identify concerns only. No investigation, no
publishing, no verdict.

Work this way:

1. Read the diff with read_file / read_outline as much as you need
   to understand the shape of the change.
2. Form distinct concerns the way you normally would — one line of
   inquiry per risk area, scaled to diff size.
3. Call `reflect(concerns=[...])` once with the full list. Each
   concern entry: `{title, description}` — short working titles +
   one-sentence description per concern.
4. Call `done(findings=[])` and exit.

What NOT to use this run (these tools are available capabilities,
but the task this run does NOT need them):
- `spawn_agent` — no investigation. Stop at concerns; do not
  delegate any to investigators. The task ends after reflect().
- `post_comment` — no findings to publish. Concerns are not
  findings; don't try to post them as inline comments.
- `set_review_status` — no verdict. The task isn't deciding
  approval; that's a separate run.

If you accidentally call any of the above, you've gone past the
task. Stop, call done(findings=[]).

The concerns you list in `reflect()` are the test output. That's
all this run measures.
