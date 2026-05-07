Your task this run is concerns identification only — no investigation,
no findings, no verdict.

Steps:

1. Read the diff with read_file / read_outline as much as you need
   to understand the shape of the change.
2. Form distinct concerns the way you normally would — one line of
   inquiry per risk area, scaled to diff size.
3. Call `reflect(concerns=[...])` once with the full list. Each
   concern entry: `{title, description}` — short, working titles.
4. Call `done(findings=[])` and exit.

Hard rules for this run:
- Do NOT call `spawn_agent` (no investigation today).
- Do NOT call `post_comment` (no findings to publish).
- Do NOT call `set_review_status` (no verdict).
- If you accidentally spawn an investigator and it returns empty
  findings, ignore that — it's expected. Do not "go investigate
  yourself" instead.

The concerns you list in `reflect()` are the test output. That's all
this run measures.
