# What changed

{diff_summary}

# Existing review comments

{existing_comments}

# Commits *(oldest → newest)*

{commits}

# Task: review this PR end-to-end

Work in three phases, in order.

## LOOK

Read the changed files. Skim outlines for the shape. Notice what
kind of change this is — hotfix, refactor, feature — and what risk
it carries. Skip generated/boilerplate (lock files, gradle wrapper,
vendored configs).

Name the concerns worth investigating — distinct lines of inquiry,
scaled to diff size: a one-line fix earns one concern, a sweeping
refactor a handful. Concerns are stable working titles, not running
summaries.

Call `reflect()` with the concerns list.

## INVESTIGATE

You have investigators at your disposal. Spawn one per concern via
`spawn_agent(agent="investigator", focus="...")` — the focus
string is your concern phrased as an investigation brief. Multiple
`spawn_agent` calls in the same step run in parallel.

```text
spawn_agent(
  agent="investigator",
  focus="BUSINESS LOGIC: Investigate the null check for
    order.getItems() in cancelOrder. Check if items can ever be
    null given the data model, whether the check is consistent
    with other methods, and what happens when inventory release
    is skipped."
)
```

Investigators return findings with evidence. Investigation is one
round — once results land, you're done investigating.

## JUDGE

For each concern, write the answer the evidence gives. No new
concerns at this stage; answer from what came back.

Handle existing PR threads. Where the diff already addresses an
open comment, react `thumbs_up`; where the fix is incomplete,
`post_comment` with `parent_id`. Don't restate that conversation.

Consolidate the investigators' findings — merge duplicates by
*same place + same problem*, keep the higher severity. Different
defects in the same area stay separate.

Publish each consolidated finding via
`post_comment(file, line, severity, text)`. Listing them all in a
single step is fine — parallel dispatch.

Call `set_review_status` with your verdict, then `done(findings)`.
