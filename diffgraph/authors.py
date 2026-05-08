"""Author resolution — single abstraction over bench-mode prefixes
and production-mode Bitbucket slugs.

In production each comment carries a real Bitbucket account
(`author` / `author_slug`). In benchmark / simulation mode all
comments are posted under one account but speakers are distinguished
by a `[<name>]` prefix in the comment text and a regex captured by
`subject_pattern`.

`resolve_author` collapses both flows into one shape:
  - display_name : label to render as the author
  - is_self      : whether this comment was authored by `bot_user`
  - body         : text with any subject-prefix stripped

Callers of this layer (THREAD render, EXISTING COMMENTS render,
anything else that needs to attribute a comment) don't have to know
whether the speakers were simulated or real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ResolvedAuthor:
    display_name: str
    is_self: bool
    body: str


_PATTERN_CACHE: dict[str, re.Pattern | None] = {}


def _compile(pattern: str) -> re.Pattern | None:
    if pattern in _PATTERN_CACHE:
        return _PATTERN_CACHE[pattern]
    try:
        compiled: re.Pattern | None = re.compile(pattern)
    except re.error:
        compiled = None
    _PATTERN_CACHE[pattern] = compiled
    return compiled


def resolve_author(
    comment: dict,
    bot_user: str = "",
    subject_pattern: str | re.Pattern | None = None,
) -> ResolvedAuthor:
    """Normalise a comment's authorship.

    `comment` is expected to look like the records produced by
    `bitbucket.get_pr_comments`: at minimum {text, author, author_slug}.
    Missing keys default to empty strings.
    """
    text = str(comment.get("text", ""))

    pat: re.Pattern | None
    if isinstance(subject_pattern, str):
        pat = _compile(subject_pattern) if subject_pattern else None
    else:
        pat = subject_pattern

    synth_name = ""
    body = text
    if pat:
        m = pat.match(text)
        if m and m.groups():
            synth_name = (m.group(1) or "").strip()
            body = text[m.end():].lstrip()

    real_name = (comment.get("author") or "").strip()
    real_slug = (comment.get("author_slug") or "").strip()

    display = synth_name or real_name or real_slug or "unknown"

    is_self = bool(
        bot_user
        and (
            (synth_name and synth_name == bot_user)
            or (not synth_name and real_slug == bot_user)
        )
    )

    return ResolvedAuthor(display_name=display, is_self=is_self, body=body)
