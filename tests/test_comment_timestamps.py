"""Comment timestamps in `read_thread` / `read_comment` / `list_threads`.

Reviewer needs to compare its prior [SELF] reply against the latest
commit on the PR to decide whether the reply is still current or
needs a refresh. To do that the agent has to see WHEN each comment
was posted. We surface Bitbucket Server's `createdDate` (ms since
UTC epoch) as a compact ISO header on every rendered comment.

Pinned format: `=== #<id> by <author> · YYYY-MM-DDThh:mmZ · <position> ...`.
The agent prompt teaches the model to read that field; if the format
drifts, the prompt's staleness rule silently breaks.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from diffgraph.comment_tools import list_threads, read_thread, read_comment


def _ms(dt_str: str) -> int:
    """Parse `2026-05-10T14:00Z` → ms since epoch (UTC)."""
    dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@pytest.fixture
def sample_comments() -> list[dict]:
    """Two comments in one thread: a root by Alice (2026-05-01) and
    a reply by [SELF] bot (2026-05-05). Plus a single-comment thread
    by Bob on a different file (2026-05-03)."""
    return [
        {
            "id": 101, "parent_id": None, "depth": 0,
            "file": "src/X.java", "line": 10,
            "text": "[BLOCKER] NPE on line 47", "author": "Alice",
            "author_slug": "alice",
            "resolved": False, "anchored": True,
            "created_ms": _ms("2026-05-01T10:00Z"),
        },
        {
            "id": 102, "parent_id": 101, "depth": 1,
            "file": "src/X.java", "line": 10,
            "text": "Confirmed, evidence in OrderService.java:120",
            "author": "diffgraph-bot", "author_slug": "diffgraph-bot",
            "resolved": False, "anchored": True,
            "created_ms": _ms("2026-05-05T14:30Z"),
        },
        {
            "id": 200, "parent_id": None, "depth": 0,
            "file": "src/Y.java", "line": 5,
            "text": "[MINOR] inconsistent indentation", "author": "Bob",
            "author_slug": "bob",
            "resolved": False, "anchored": True,
            "created_ms": _ms("2026-05-03T09:15Z"),
        },
    ]


class TestReadThreadTimestamps:

    def test_header_contains_timestamp(self, sample_comments):
        out = read_thread(
            sample_comments, comment_id=101,
            snapshot_max_id_value=999, bot_user="diffgraph-bot",
        )
        # Root posted 2026-05-01T10:00Z
        assert "2026-05-01T10:00Z" in out
        # Reply posted 2026-05-05T14:30Z
        assert "2026-05-05T14:30Z" in out

    def test_self_reply_carries_timestamp(self, sample_comments):
        """The whole point of the timestamp is so reviewer can spot
        whether its OWN past reply is stale. The [SELF] tag and the
        timestamp must coexist in the same header line."""
        out = read_thread(
            sample_comments, comment_id=101,
            snapshot_max_id_value=999, bot_user="diffgraph-bot",
        )
        # Find the [SELF] line and check that the SAME line carries
        # the timestamp.
        self_lines = [
            line for line in out.splitlines()
            if "[SELF]" in line and line.startswith("=== #102")
        ]
        assert len(self_lines) == 1, f"expected one [SELF] header, got:\n{out}"
        assert "2026-05-05T14:30Z" in self_lines[0]

    def test_missing_created_ms_renders_without_timestamp(self):
        """Older fakes / fixtures that don't set `created_ms` must
        still render — no NoneType error, just no timestamp part."""
        comments = [{
            "id": 1, "parent_id": None, "depth": 0,
            "text": "old comment", "author": "x", "author_slug": "x",
            "file": "", "line": 0, "resolved": False, "anchored": False,
            # no created_ms
        }]
        out = read_thread(comments, comment_id=1, snapshot_max_id_value=99)
        assert "old comment" in out
        # No date markers leaked.
        assert "T" not in out.split("===")[1] or "1970" not in out


class TestReadCommentTimestamp:
    def test_single_comment_header_has_timestamp(self, sample_comments):
        out = read_comment(
            sample_comments, comment_id=101,
            snapshot_max_id_value=999, bot_user="diffgraph-bot",
        )
        assert "2026-05-01T10:00Z" in out
        # Sanity: still has the rest of the header.
        assert "#101" in out
        assert "Alice" in out


class TestAnchorRendering:
    """Comment anchors carry the source-commit SHA they peg to plus
    an `orphaned` flag set by Bitbucket Server when subsequent
    commits removed the anchored line. Both are surfaced in the
    header so the reviewer can spot threads anchored on stale code
    (= older commits or removed lines) before deciding to reply."""

    def _anchored(self, **overrides) -> dict:
        c = {
            "id": 1, "parent_id": None, "depth": 0,
            "file": "src/X.java", "line": 47,
            "text": "blocker on this line",
            "author": "Alice", "author_slug": "alice",
            "resolved": False, "anchored": True,
            "anchor_to_hash": "abcdef1234567890",
            "anchor_orphaned": False,
            "created_ms": None,
        }
        c.update(overrides)
        return c

    def test_anchor_shows_short_sha(self):
        out = read_thread([self._anchored()], comment_id=1,
                          snapshot_max_id_value=99)
        # Path:line followed by @<7-char short sha>.
        assert "src/X.java:47@abcdef1" in out

    def test_outdated_anchor_marked(self):
        c = self._anchored(anchor_orphaned=True)
        out = read_thread([c], comment_id=1, snapshot_max_id_value=99)
        assert "(outdated)" in out

    def test_non_outdated_no_tag(self):
        out = read_thread([self._anchored()], comment_id=1,
                          snapshot_max_id_value=99)
        assert "(outdated)" not in out

    def test_missing_anchor_to_hash_renders_path_only(self):
        c = self._anchored(anchor_to_hash="")
        out = read_thread([c], comment_id=1, snapshot_max_id_value=99)
        # No `@<sha>` clause, but the path:line still shows.
        assert "src/X.java:47" in out
        assert "@" not in out.split("src/X.java:47", 1)[1].split("===")[0]

    def test_unanchored_comment_no_anchor_clause(self):
        """General PR comments (no file/line) get no `@ <path>` part
        at all — the renderer skips the whole clause."""
        c = self._anchored(file="", line=0, anchored=False)
        out = read_thread([c], comment_id=1, snapshot_max_id_value=99)
        # The `@` wouldn't appear in author/header without an anchor.
        # Sanity check: file path is empty so the clause is omitted.
        assert "src/X.java" not in out

    def test_anchor_in_read_comment(self):
        """Same anchor rendering applies to `read_comment` (single
        comment, full body, used when read_thread truncated)."""
        out = read_comment([self._anchored(anchor_orphaned=True)],
                           comment_id=1, snapshot_max_id_value=99)
        assert "src/X.java:47@abcdef1" in out
        assert "(outdated)" in out


class TestListThreadsTimestamps:
    def test_thread_summary_shows_last_activity(self, sample_comments):
        """Reviewer scrolling `list_threads` should see "last
        <timestamp>" so it knows which threads were touched
        recently."""
        out = list_threads(
            sample_comments, snapshot_max_id_value=999,
            bot_user="diffgraph-bot",
        )
        # Thread 101: last activity is the SELF reply on 2026-05-05.
        # Thread 200: only the root on 2026-05-03.
        assert "last 2026-05-05T14:30Z" in out
        assert "last 2026-05-03T09:15Z" in out

    def test_self_tag_and_timestamp_compose(self, sample_comments):
        """`[SELF in subtree]` and the `last <ts>` tag both belong
        on the same summary line."""
        out = list_threads(
            sample_comments, snapshot_max_id_value=999,
            bot_user="diffgraph-bot",
        )
        line = next(
            line for line in out.splitlines()
            if line.startswith("#101")
        )
        assert "[SELF in subtree]" in line
        assert "last 2026-05-05T14:30Z" in line
