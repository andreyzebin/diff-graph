"""Comment-graph navigation primitives for agents.

Three tools — `list_threads`, `read_thread`, `read_comment` — let an
agent paginate the PR's comment graph on demand instead of seeing a
single pre-rendered EXISTING COMMENTS string in the prompt. The
old static rendering is the source of "thread crosstalk" — the
LLM drifts into a sibling thread's topic when it can see all of
them at once. Tools shift that decision to the agent.

Snapshot semantics
------------------
At ctx creation we cache `max_comment_id` — the highest comment id
present at the start of the run. All three tools filter to
`id <= max_id`, so comments the agent itself posts mid-run (and
which receive new monotonic ids) are NOT visible via these tools.
The agent already has its own tool-call history; surfacing its
post_comment outputs again here would create a moving target.

Identity
--------
Each comment runs through `resolve_author` to apply the same
two-stage rule we use elsewhere (subject_pattern persona override,
then bot_user [SELF] tag). Bench-seeded `[alice] / [bob]` style
threads keep working unchanged.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


def _format_anchor(c: dict) -> str:
    """Render an inline comment's anchor as a trailing ` @ <path>:
    <line>` clause for the comment header. When the anchor pegs to a
    specific source commit, append the short SHA so the reviewer can
    tell whether the thread is talking about the current tip or an
    older revision. When Bitbucket flagged the anchor as `orphaned`
    (the line was removed by subsequent commits) tag the clause
    `(outdated)` — that's a strong signal the discussion may not
    still apply.

    Empty string when the comment isn't anchored to a file (general
    PR comments and most thread replies — Bitbucket only attaches
    `commentAnchor` to the root inline comment).
    """
    if not c.get("file"):
        return ""
    anchor = f" @ {c['file']}:{c.get('line') or 0}"
    sha = (c.get("anchor_to_hash") or "").strip()
    if sha:
        anchor += f"@{sha[:7]}"
    if c.get("anchor_orphaned"):
        anchor += " (outdated)"
    return anchor


def _fmt_created(c: dict) -> str:
    """Render a comment's `created_ms` (milliseconds since UTC epoch,
    as returned by Bitbucket Server) as a compact ISO date+time the
    LLM can compare against commit timestamps to detect stale prior
    replies ("my SELF comment from 2026-05-01 is older than the
    latest commit on 2026-05-10 → my confirmation may not still hold").

    Returns "" when `created_ms` is missing/unparsable — older fakes
    or benches that don't populate the field render unchanged.
    """
    ms = c.get("created_ms")
    if ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
        # ISO-8601 to the minute, UTC. Seconds are noise for staleness
        # decisions; commit times have similar resolution.
        return dt.strftime("%Y-%m-%dT%H:%MZ")
    except (TypeError, ValueError):
        return ""

from .authors import resolve_author


def snapshot_max_id(comments: list[dict]) -> int:
    """Highest id in the comment list. 0 if no comments yet."""
    ids = [c.get("id") for c in comments if c.get("id")]
    return max(ids) if ids else 0


def _filter_snapshot(comments: list[dict], max_id: int) -> list[dict]:
    if not max_id:
        return list(comments)
    return [c for c in comments if c.get("id") and c["id"] <= max_id]


def _root_of(node: dict, by_id: dict[int, dict]) -> int | None:
    cur = node
    seen: set[int] = set()
    while cur.get("parent_id") and cur["parent_id"] in by_id:
        cid = cur.get("id")
        if cid in seen:
            break
        seen.add(cid)
        cur = by_id[cur["parent_id"]]
    return cur.get("id")


def _build_children_map(comments: list[dict]) -> dict[int, list[dict]]:
    children_of: dict[int, list[dict]] = {}
    for c in comments:
        pid = c.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(c)
    # IDs are monotonic, so sorting by id gives chronological order.
    for kids in children_of.values():
        kids.sort(key=lambda c: c.get("id") or 0)
    return children_of


def _author_label(c: dict, bot_user: str, pat) -> tuple[str, str]:
    """Return (author_label, body_with_persona_prefix_stripped)."""
    a = resolve_author(c, bot_user=bot_user, subject_pattern=pat)
    return ("[SELF]" if a.is_self else a.display_name), a.body


def list_threads(
    comments: list[dict],
    snapshot_max_id_value: int,
    bot_user: str = "",
    subject_pattern: Optional[re.Pattern] = None,
    start: int = 0,
    n: int = 30,
    sort: str = "newest",
) -> str:
    """List root threads. Each row is a single line so the agent can
    eyeball the whole PR at a glance and pick which thread to expand
    via `read_thread`.

    sort: "newest" (default — by descending root id), "most_active"
    (by descending reply count), "oldest" (by ascending root id).
    """
    comments = _filter_snapshot(comments, snapshot_max_id_value)
    by_id = {c["id"]: c for c in comments if c.get("id")}

    roots_map: dict[int, list[dict]] = {}
    for c in comments:
        rid = _root_of(c, by_id)
        if rid is None:
            continue
        roots_map.setdefault(rid, []).append(c)

    summaries = []
    for rid, group in roots_map.items():
        root = by_id.get(rid)
        if not root:
            continue
        author_label, body = _author_label(root, bot_user, subject_pattern)
        first_line = body.splitlines()[0] if body else ""
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        n_replies = len(group) - 1
        has_self_in_subtree = any(
            resolve_author(c, bot_user=bot_user, subject_pattern=subject_pattern).is_self
            for c in group
        )
        # Latest activity in the subtree — `created_ms` is the
        # comment-creation timestamp. Use the max across the root +
        # all replies so the listing reflects when the thread was
        # LAST touched, not when it was opened.
        latest_ms = max(
            (c.get("created_ms") for c in group if c.get("created_ms") is not None),
            default=None,
        )
        anchor = ""
        if root.get("file"):
            anchor = f" @ {root['file']}:{root.get('line') or 0}"
        summaries.append({
            "id": rid,
            "author": author_label,
            "first_line": first_line,
            "n_replies": n_replies,
            "has_self": has_self_in_subtree,
            "anchor": anchor,
            "latest_ts": _fmt_created({"created_ms": latest_ms}),
        })

    if sort == "most_active":
        summaries.sort(key=lambda s: -s["n_replies"])
    elif sort == "oldest":
        summaries.sort(key=lambda s: s["id"])
    else:
        summaries.sort(key=lambda s: -s["id"])

    total = len(summaries)
    page = summaries[start:start + n]

    out: list[str] = []
    out.append(
        f"=== {total} thread(s) on PR (snapshot_max_id={snapshot_max_id_value}); "
        f"showing {start}–{start + len(page) - 1 if page else start} ==="
    )
    out.append("")
    if not page:
        out.append("(no threads in this page)")
        return "\n".join(out)

    for s in page:
        self_tag = " · [SELF in subtree]" if s["has_self"] else ""
        replies_str = (
            "leaf" if s["n_replies"] == 0
            else (f"{s['n_replies']} reply" if s["n_replies"] == 1
                  else f"{s['n_replies']} replies")
        )
        ts_part = f" · last {s['latest_ts']}" if s["latest_ts"] else ""
        out.append(f"#{s['id']} by {s['author']} · {replies_str}{s['anchor']}{self_tag}{ts_part}")
        out.append(f"  {s['first_line']}")

    has_more = start + len(page) < total
    if has_more:
        out.append("")
        out.append(
            f"[{total - start - len(page)} more — "
            f"call list_threads(start={start + len(page)})]"
        )
    return "\n".join(out)


def read_thread(
    comments: list[dict],
    comment_id: int,
    snapshot_max_id_value: int,
    bot_user: str = "",
    subject_pattern: Optional[re.Pattern] = None,
    per_comment_cap_chars: int = 2000,
    per_thread_cap_chars: int = 12000,
) -> str:
    """Render the FULL thread containing `comment_id`, depth-first.

    Pass any comment id (root, leaf, mid-tree) — the tool finds the
    root, walks the subtree depth-first, and renders each comment
    as a header block followed by its verbatim body. The passed id
    is marked `← FOCUS` in the headers so the agent can locate
    itself inside a long thread.

    Caps:
      - per-comment body cap (~2000 chars by default). Longer bodies
        get truncated with a hint to call `read_comment(id)` for the
        full text.
      - per-thread cumulative cap (~12000 chars). When exceeded, the
        rest of the depth-first walk is skipped with a hint to call
        `read_thread(<sub_id>)` to explore further.
    """
    comments = _filter_snapshot(comments, snapshot_max_id_value)
    by_id = {c["id"]: c for c in comments if c.get("id")}
    if comment_id not in by_id:
        return (
            f"(comment #{comment_id} not found in snapshot — "
            f"either it doesn't exist or it was created after run start; "
            f"snapshot_max_id={snapshot_max_id_value})"
        )

    rid = _root_of(by_id[comment_id], by_id)
    children_of = _build_children_map(comments)

    def walk(node_id: int, depth: int, acc: list[tuple[dict, int]]) -> None:
        node = by_id.get(node_id)
        if node is None:
            return
        acc.append((node, depth))
        for ch in children_of.get(node_id, []):
            walk(ch["id"], depth + 1, acc)

    walk_order: list[tuple[dict, int]] = []
    walk(rid, 0, walk_order)

    out: list[str] = []
    out.append(
        f"=== Thread rooted at #{rid} · "
        f"{len(walk_order)} comment(s) · focus: #{comment_id} ==="
    )
    out.append("")

    total_chars = 0
    for idx, (c, _depth) in enumerate(walk_order):
        author_label, body = _author_label(c, bot_user, subject_pattern)
        n_kids = len(children_of.get(c["id"], []))
        parent_id = c.get("parent_id")
        position = "root" if not parent_id else f"reply to #{parent_id}"
        if n_kids == 0:
            replies_str = "leaf"
        elif n_kids == 1:
            replies_str = "1 reply"
        else:
            replies_str = f"{n_kids} replies"
        anchor = _format_anchor(c)
        focus_marker = " ← FOCUS" if c["id"] == comment_id else ""
        ts = _fmt_created(c)
        ts_part = f" · {ts}" if ts else ""

        out.append(
            f"=== #{c['id']} by {author_label}{ts_part} · {position} · "
            f"{replies_str}{anchor}{focus_marker} ==="
        )
        if len(body) > per_comment_cap_chars:
            shown = body[:per_comment_cap_chars]
            out.append(shown)
            out.append(
                f"[body truncated at {per_comment_cap_chars} chars; "
                f"call read_comment({c['id']}) for the full text]"
            )
        else:
            out.append(body)
        out.append("")
        total_chars += len(body)
        if total_chars > per_thread_cap_chars and idx + 1 < len(walk_order):
            remaining = len(walk_order) - idx - 1
            out.append(
                f"[{remaining} more comment(s) in this thread truncated; "
                f"call read_thread(<sub_id>) on a specific branch to expand]"
            )
            break

    return "\n".join(out).rstrip()


def read_comment(
    comments: list[dict],
    comment_id: int,
    snapshot_max_id_value: int,
    bot_user: str = "",
    subject_pattern: Optional[re.Pattern] = None,
) -> str:
    """Render ONE comment, full body, no caps. For the rare case
    where `read_thread`'s per-comment cap truncated something the
    agent really needs to see in full.
    """
    comments = _filter_snapshot(comments, snapshot_max_id_value)
    by_id = {c["id"]: c for c in comments if c.get("id")}
    if comment_id not in by_id:
        return (
            f"(comment #{comment_id} not found in snapshot; "
            f"snapshot_max_id={snapshot_max_id_value})"
        )

    c = by_id[comment_id]
    author_label, body = _author_label(c, bot_user, subject_pattern)
    parent_id = c.get("parent_id")
    position = "root" if not parent_id else f"reply to #{parent_id}"
    anchor = _format_anchor(c)
    ts = _fmt_created(c)
    ts_part = f" · {ts}" if ts else ""
    return f"=== #{c['id']} by {author_label}{ts_part} · {position}{anchor} ===\n{body}"
