from __future__ import annotations

import itertools

from fake_servers.providers.base import (
    BitbucketWriteSink, CommentAnchor, CommentThread, ReviewStatus, CapturedOutput
)

_id_counter = itertools.count(1)


class InMemoryWriteSink(BitbucketWriteSink):
    """In-memory implementation of BitbucketWriteSink for benchmark testing."""

    def __init__(self):
        self._comments: list[CommentThread] = []
        self._review_status: ReviewStatus | None = None

    async def add_comment(
        self, pr_id: int, text: str, anchor: CommentAnchor | None
    ) -> CommentThread:
        thread = CommentThread(
            id=next(_id_counter),
            text=text,
            anchor=anchor,
        )
        self._comments.append(thread)
        return thread

    async def set_review_status(self, pr_id: int, status: str) -> ReviewStatus:
        rs = ReviewStatus(status=status)
        self._review_status = rs
        return rs

    async def get_captured(self) -> CapturedOutput:
        return CapturedOutput(
            comments=list(self._comments),
            review_status=self._review_status,
        )

    async def reset(self) -> None:
        self._comments.clear()
        self._review_status = None
