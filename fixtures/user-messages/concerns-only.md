---
tools: [diff_list_files, diff_read_file, diff_outline, diff_search, reflect, done]
---
PR: {pr_title}
{pr_description}

Commits *(oldest → newest)*:

{commits}

Existing threads on this PR:

{existing_comments}

Identify the concerns this diff raises. Read the diff to
understand the shape of the change, form distinct concerns
(one line of inquiry per risk area, scaled to diff size),
then call reflect(questions_remaining=[{id, text}, ...]) with
each concern phrased as an investigation question. Finish
with done(findings=[]).

The questions in your reflect() are the run's output — that's
all this task measures.
