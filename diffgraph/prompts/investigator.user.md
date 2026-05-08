# What changed

{diff_summary}

# Your concern

{focus}

# Commits *(oldest → newest)*

{commits}

# Task: investigate the concern above

Workflow:

1. **Start** by reading changes for files relevant to your concern:
   `read_file(path, changes_only=true, before=3, after=3)` for each file
   from *What changed* that relates to your focus. This shows only
   diff hunks. Also call `read_outline()` on key files. Gather facts
   before reflecting.

2. **Then** call `reflect()` with:

   - `learned` — facts you established from the code you just read
   - `questions_remaining` — only questions you genuinely need to investigate
     further. Do NOT list questions you can already answer from what you read.
   - `confidence` — your current assessment

3. **Investigate** remaining questions with tools. Follow call chains,
   check related code, verify assumptions.

4. **`reflect()` every 3–5 tool calls** to track progress:

   - Move answered questions to `resolved_questions` with the answer.
   - Keep `questions_remaining` for things you still need to check.

5. **Optional dedup against existing PR discussion** — if your
   focus could plausibly be already raised, call `list_threads()`
   once and `read_thread(<id>)` on anything that looks related.
   Don't re-report issues already covered by an open thread; cite
   the thread in your finding's `evidence` if relevant. Skip this
   step entirely when the focus is clearly novel.

6. Call `done(findings)` when all questions are answered or budget
   is running low.
