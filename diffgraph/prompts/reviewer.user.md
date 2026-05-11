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

Call `reflect(...)` listing each concern under `questions_remaining`
as `{id, text}` — `text` is the concern phrased as an investigation
question (e.g. *"Does FUNCTION_X handle EDGE_CASE_Y correctly?"*
or *"Is INVARIANT_Z preserved across BOUNDARY_W?"* — these are
generic placeholders; the actual concern names come from the diff).

## INVESTIGATE

You have investigators at your disposal. Spawn one per concern via
`spawn_agent(agent="investigator", focus="...")` — the focus
string is your concern phrased as an investigation brief. Multiple
`spawn_agent` calls in the same step run in parallel.

```text
# Generic shape — substitute domain + symbol + the concrete question.
spawn_agent(
  agent="investigator",
  focus="DOMAIN_AREA: Investigate <one specific question about a
    symbol in the diff>. Check <constraint A> and <invariant B>,
    and what happens when <edge condition>."
)
```

Investigators return findings with evidence. Investigation is one
round — once results land, you're done investigating.

## JUDGE

For each concern, write the answer the evidence gives. No new
concerns at this stage; answer from what came back.

If you want to dedup against the PR's existing discussion, call
`list_threads()` once to see roots, then `read_thread(<id>)` for
any that look related. Where the diff already addresses an open
thread, `react_to_comment` with `thumbs_up`; where the fix is
incomplete, `post_comment` with `parent_id`. Don't restate that
conversation. If your finding has nothing to do with prior
discussion, you don't need to look at all.

Consolidate the investigators' findings — merge duplicates by
*same place + same problem*, keep the higher severity. Different
defects in the same area stay separate.

Publish each consolidated finding via
`post_comment(file, line, severity, text)`. Listing them all in a
single step is fine — parallel dispatch.

Call `set_review_status` with your verdict, then `done(findings)`.
