---
dispatch_mode: native
# Production review uses every reviewer capability — add the thread-
# reading + delegation + publishing tools on top of the base diff
# toolkit (declared in reviewer.system.md frontmatter).
tools_add: [list_threads, read_thread, read_comment, react_to_comment,
            list_agents, spawn_agent, post_comment, set_review_status]
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

Review this PR end-to-end.

Read the diff to understand the change, identify concerns, then
spawn investigators (spawn_agent) for any concern that needs depth.
When their findings come back, consolidate (merge duplicates, keep
the higher severity, drop anything already covered in an open
thread). Publish each finding via post_comment(file, line, severity,
text), set the verdict via set_review_status(APPROVED|NEEDS_WORK,
reason), and finish with done(findings).
