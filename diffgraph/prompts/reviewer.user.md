WHAT CHANGED:
{diff_summary}

EXISTING REVIEW COMMENTS:
{existing_comments}

COMMITS (oldest → newest):
{commits}

Review this PR end-to-end. A review unfolds in three movements:
looking at the change, asking focused questions about it, and coming
to a verdict. They run in sequence — once you're judging, you're
past investigating.

LOOK
  Read the changed files. Skim outlines for the shape. Notice what
  kind of change this is — hotfix, refactor, feature — and what risk
  it carries. Skip generated/boilerplate (lock files, gradle wrapper,
  vendored configs).

  From that look, name the concerns worth investigating. Each concern
  is a distinct line of inquiry — a risk area, a question of
  correctness, a place where the change might conflict with the
  codebase's conventions. Scale to the diff: a one-line fix earns one
  concern, a sweeping refactor a handful. Concerns are stable working
  titles, not running summaries; once written, leave them as is.

  Then call reflect() with the concerns you'll investigate.

INVESTIGATE
  Spawn one investigator per concern. With multiple concerns, emit
  all spawn_agent calls in the same step — they run in parallel.

    spawn_agent(agent="investigator",
      focus="BUSINESS LOGIC: Investigate the null check for
        order.getItems() in cancelOrder. Check if items can ever be
        null given the data model, whether the check is consistent
        with other methods, and what happens when inventory release
        is skipped.")

  Investigators come back with their own findings and evidence.
  Investigation is one round — once results land, you move on.

JUDGE
  Reflect on what came back. For each concern, write the answer the
  evidence gives: "MAJOR: null check hides a data-integrity issue —
  items are guaranteed non-null per @Builder.Default; the guard
  silences a mapping bug." No new concerns at this stage; answer
  from the evidence you have.

  Handle existing PR threads. Where the diff already addresses a
  comment, react thumbs_up; where the fix is incomplete, post_comment
  with parent_id. Don't restate that conversation in your findings.

  Consolidate investigators' findings into one review. Each was
  already filtered for evidence — your role is to weave the sets
  together, not to re-judge them. Two findings describe the same
  defect when they point at the same place in the code and the same
  problem; merge those, keeping the clearer evidence and the higher
  severity. Different defects in the same area stay separate.

  Publish each consolidated finding through post_comment(text, file,
  line, severity). Listing them all in a single step is fine — the
  framework dispatches parallel tool calls. A finding written but
  not posted is a finding the team never sees; done() alone doesn't
  publish.

  Set the review status. Call done(findings).
