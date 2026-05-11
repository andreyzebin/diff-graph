"""AgentPRView backed by a fake-bitbucket sink + payload.

Used by the unit-tier runner (`run_unit.py`): after the agent
subprocess finishes, we have a list of sink records (what the
agent posted/reacted/set_status'd) and the original payload
(repo path, base/source shas, seed comments). LLMJudge needs an
`AgentPRView` to read the agent's outputs back; this class
synthesises that view from those two sources — no network, no
real Bitbucket required.

Mirrors `RealBitbucketPRProxy` from `bitbucket/real_factory.py` in
shape, but every read is a pure-Python lookup. Methods are async
to satisfy the `AgentPRView` interface — they don't actually do
any awaiting.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from bitbucket.base import (
    AgentPRView,
    CommentAnchor,
    CommentThread,
    ReviewStatus,
)


# Sink record `kind` values that represent agent-authored comments
# the judge should treat as visible PR output. `react` and `resolve`
# are agent actions on existing comments, not new comments.
_COMMENT_KINDS = ("post_comment", "post_general", "review_comment", "reply")


def _seed_to_thread(c: dict) -> CommentThread:
    """Convert a normalised seed-comment dict (from FakeBitbucket payload)
    into a CommentThread the judge can consume.

    The payload's `_normalise_comment` already canonicalised the shape
    to {id, file, line, text, author_slug, …}. We map it onto the
    bench's CommentThread / CommentAnchor dataclasses."""
    file_path = c.get("file") or ""
    if file_path:
        anchor = CommentAnchor(
            path=file_path,
            line=int(c.get("line") or 0),
            line_type="ADDED",
        )
    else:
        anchor = None
    return CommentThread(
        id=int(c.get("id") or 0),
        text=str(c.get("text") or ""),
        anchor=anchor,
    )


def _sink_record_to_thread(rec: dict) -> CommentThread:
    """Convert one agent sink record into a CommentThread the judge
    consumes. The four kinds in `_COMMENT_KINDS` produce a thread;
    other kinds (react/resolve/set_status) are filtered upstream."""
    kind = rec.get("kind", "")
    file_path = rec.get("file") or ""
    if kind == "post_comment" and file_path:
        anchor: Optional[CommentAnchor] = CommentAnchor(
            path=file_path,
            line=int(rec.get("line") or 0),
            line_type=str(rec.get("line_type") or "ADDED"),
        )
    elif kind == "review_comment" and file_path:
        anchor = CommentAnchor(
            path=file_path,
            line=int(rec.get("line") or 0),
            line_type="ADDED",
        )
    else:
        anchor = None
    severity = str(rec.get("severity") or "").upper() or "NORMAL"
    if severity not in ("NORMAL", "BLOCKER"):
        severity = "NORMAL"
    return CommentThread(
        id=int(rec.get("new_id") or 0),
        text=str(rec.get("text") or ""),
        anchor=anchor,
        severity=severity,
    )


class FakeBenchPRView(AgentPRView):
    """Read-only view over the fake-PR state used by `bench run-unit`.

    Constructed AFTER the agent subprocess finishes — we have the full
    sink in hand at that point. Reads back the agent's posted comments
    + set_status events so LLMJudge.evaluate() can do its scoring just
    like it does for the real-Bitbucket integration tier.
    """

    def __init__(
        self,
        *,
        payload: dict,
        sink_records: list[dict],
        repo_path: Path,
        base_sha: str,
        source_sha: str,
        source_branch: str = "",
        agent_user: str = "",
    ) -> None:
        self.payload = payload
        self.sink_records = list(sink_records)
        self.repo_path = Path(repo_path)
        self.base_sha = base_sha
        self.source_sha = source_sha
        self.source_branch = source_branch
        # Whose comments to surface in get_comments(). For unit fixtures
        # the agent IS the bot account; the judge filters seed comments
        # by id, not by author, so this field is mostly informational.
        self.agent_user = agent_user or str(
            (payload.get("metadata") or {}).get("bot_user")
            or payload.get("self_user") or "diffgraph-bot"
        )
        # parse_pr_url's fallback returns ("FAKE", "fake-repo", 0) for
        # the bench's fake://… URLs; the test framework relies on
        # pr_id being any int, so 0 is fine.
        self._pr_id = int(
            (payload.get("metadata") or {}).get("pr_id")
            or 0
        )
        self._pr_url = str(payload.get("pr_url") or "")
        self._closed = False

    # ── identity ──────────────────────────────────────────────────────

    @property
    def pr_id(self) -> int:
        return self._pr_id

    @property
    def pr_url(self) -> str | None:
        return self._pr_url or None

    async def close(self) -> None:
        # No resources to release — sink + payload are Python objects
        # the runner cleans up after we're done. Idempotent.
        self._closed = True

    # ── comments ──────────────────────────────────────────────────────

    async def get_comments(self) -> list[CommentThread]:
        """Comments the AGENT posted on this PR — the four sink kinds
        in `_COMMENT_KINDS`. Filters out reactions / resolves /
        verdict events (those have dedicated channels). Seed
        comments from the fixture's payload are NOT included here —
        get_all_comments() includes them."""
        return [
            _sink_record_to_thread(rec)
            for rec in self.sink_records
            if rec.get("kind") in _COMMENT_KINDS
        ]

    async def get_all_comments(self) -> list[CommentThread]:
        """Every comment on the PR, regardless of author. Seed
        comments from the fixture's `pr_state.comments` PLUS the
        agent's posts. The judge's reply-scoring path uses this to
        see the full thread context."""
        seeds = [_seed_to_thread(c) for c in (self.payload.get("comments") or [])]
        return seeds + await self.get_comments()

    # ── review status ─────────────────────────────────────────────────

    async def get_review_status(
        self, verdict_source: str = "api",
    ) -> ReviewStatus | None:
        """Last `set_status` event the agent emitted on the PR.
        UNAPPROVED → None (matches RealBitbucketPRProxy semantics —
        "default" status counts as absent).

        verdict_source is honoured loosely: "comment" / "both" also
        scan the agent's general comments for a `[verdict:STATUS]`
        marker the same way the real proxy does, so unit fixtures can
        test either delivery channel."""
        status: Optional[str] = None
        # API channel — last set_status wins.
        if verdict_source in ("api", "both", ""):
            for rec in reversed(self.sink_records):
                if rec.get("kind") == "set_status":
                    status = str(rec.get("status") or "").upper()
                    break
        # Comment channel — scan general comments for [verdict:X] markers.
        if status in (None, "UNAPPROVED") and verdict_source in ("comment", "both"):
            for rec in self.sink_records:
                if rec.get("kind") not in ("post_general", "post_comment"):
                    continue
                text = str(rec.get("text") or "")
                if "[verdict:APPROVED]" in text:
                    status = "APPROVED"
                elif "[verdict:NEEDS_WORK]" in text:
                    status = "NEEDS_WORK"
        if not status or status == "UNAPPROVED":
            return None
        return ReviewStatus(status=status)

    # ── code grounding ────────────────────────────────────────────────

    async def get_diff(self) -> str:
        """Unified diff base..source over the temp clone. Same shape
        the agent's diff tools see — judge can ground wrong-location
        warnings against this."""
        if not (self.repo_path.exists() and self.base_sha and self.source_sha):
            return ""
        try:
            return subprocess.run(
                ["git", "diff", f"{self.base_sha}..{self.source_sha}"],
                cwd=str(self.repo_path),
                capture_output=True, text=True, check=True,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    async def get_raw_file(self, path: str, ref: str = "") -> str:
        """File content at the given ref (branch / sha). Empty ref →
        source branch tip. Used by the judge to read AGENTS.md and
        verify project-convention claims."""
        if not self.repo_path.exists():
            return ""
        target = ref or self.source_branch or self.source_sha or "HEAD"
        try:
            return subprocess.run(
                ["git", "show", f"{target}:{path}"],
                cwd=str(self.repo_path),
                capture_output=True, text=True, check=True,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    # ── lifecycle helpers ─────────────────────────────────────────────

    def exclude_seed_comment_ids(self) -> set[int]:
        """IDs of comments seeded by the fixture (not posted by the
        agent). The judge uses this to drop them from `get_comments()`
        results — though for unit fixtures we already filter strictly
        to sink-side kinds, so this set is informational only."""
        return {
            int(c.get("id") or 0)
            for c in (self.payload.get("comments") or [])
            if c.get("id") is not None
        }
