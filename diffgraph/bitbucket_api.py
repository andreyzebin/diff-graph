"""Bitbucket integration contract.

Both `diffgraph.bitbucket` (the live HTTP client) and
`diffgraph.bitbucket_fake` (yaml-fixture driven, for unit-tier
tests — TODO §5e.14) expose the functions listed below as free
module-level callables. Python's structural typing makes the swap
drop-in: the end of `bitbucket.py` rebinds these names to the
fake module when `DIFFGRAPH_FAKE_PR_FILE` is present in env.

This file is documentation + IDE navigation. Nothing enforces
the protocol at runtime.
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol, TypedDict


# ── Data shapes returned by the API ───────────────────────────────────────


class Author(TypedDict, total=False):
    name: str           # display name
    slug: str           # bitbucket username (used in @-mentions)


class PRMeta(TypedDict, total=False):
    """What `get_pr_info` / `fetch_pr`'s pr_meta returns."""
    title: str
    description: str
    base_ref: str       # SHA of the target branch tip
    source_ref: str     # SHA of the source branch tip
    state: str          # OPEN / MERGED / DECLINED
    author: Author


class Anchor(TypedDict, total=False):
    """Where an inline comment is anchored."""
    path: str
    line: int
    line_type: str      # ADDED / REMOVED / CONTEXT


class Comment(TypedDict, total=False):
    """One flat comment row. Threads are reconstructed from
    `parent_id` chains at read time — fake fixture stores a plain
    list, fake module's `get_comment_thread` traverses it."""
    id: int
    parent_id: int      # 0 = top-level comment
    author: Author
    text: str
    anchor: Anchor
    state: str          # OPEN / RESOLVED


# ── The interface ────────────────────────────────────────────────────────


class BitbucketAPI(Protocol):
    """Free-function module API. Both the real and fake modules
    expose these names with matching signatures."""

    @staticmethod
    def parse_pr_url(pr_url: str) -> tuple[str, str, str, int]:
        """Pure parser. Returns (server_url, project, repo, pr_id)."""
        ...

    @staticmethod
    def fetch_pr(
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str, Callable[[], None], PRMeta]:
        """Lazy clone + diff fetch. Returns
        (diff_text, repo_path, cleanup_fn, pr_meta)."""
        ...

    @staticmethod
    def get_pr_info(
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> PRMeta:
        ...

    @staticmethod
    def get_pr_comments(
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> list[Comment]:
        """Flat list, ordered by id ascending."""
        ...

    @staticmethod
    def get_comment_thread(
        pr_url: str,
        comment_id: int,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        bot_user: str = "",
        subject_pattern: str = "",
    ) -> str:
        """Rendered, human-readable thread starting at root of
        the chain containing `comment_id`. Returned as text the
        agent's `read_thread` tool surfaces directly."""
        ...

    # ── Write side ─────────────────────────────────────────────────────

    @staticmethod
    def reply_to_pr_comment(
        pr_url: str,
        comment_id: int,
        text: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None: ...

    @staticmethod
    def resolve_pr_comment(
        pr_url: str,
        comment_id: int,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None: ...

    @staticmethod
    def post_pr_comment(
        pr_url: str,
        *,
        text: str,
        file: str = "",
        line: int = 0,
        severity: str = "",
        parent_id: int = 0,
        line_type: str = "ADDED",
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> int:
        """Returns the new comment id."""
        ...

    @staticmethod
    def post_general_pr_comment(
        pr_url: str,
        text: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> int: ...

    @staticmethod
    def post_review_comments(
        pr_url: str,
        comments: list,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
        changed_lines: Optional[dict] = None,
        decorate: Optional[Callable[[str], str]] = None,
    ) -> int: ...

    @staticmethod
    def set_review_status(
        pr_url: str,
        user_slug: str,
        status: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None: ...
