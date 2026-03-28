from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Author:
    name: str
    display_name: str


@dataclass
class PullRequestData:
    id: int
    title: str
    description: str
    author: Author
    from_branch: str
    to_branch: str
    status: str        # OPEN | MERGED | DECLINED
    head_commit: str


@dataclass
class DiffHunk:
    old_start: int
    new_start: int
    lines: list[str]


@dataclass
class FileDiff:
    path: str
    change_type: str   # MODIFY | ADD | DELETE | RENAME
    hunks: list[DiffHunk]


@dataclass
class FileContent:
    path: str
    content: str


@dataclass
class CommentAnchor:
    path: str
    line: int
    line_type: str     # ADDED | REMOVED | CONTEXT


@dataclass
class CommentThread:
    id: int
    text: str
    anchor: CommentAnchor | None   # None → general PR comment
    severity: str = "NORMAL"       # NORMAL | BLOCKER
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ReviewStatus:
    status: str        # APPROVED | NEEDS_WORK
    set_at: datetime = field(default_factory=datetime.utcnow)


class BitbucketPRProxy(ABC):
    """Verification interface for a single PR benchmark run."""

    @property
    @abstractmethod
    def pr_id(self) -> int: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_comments(self) -> list[CommentThread]: ...

    @abstractmethod
    async def get_review_status(self) -> ReviewStatus | None: ...

    async def __aenter__(self) -> "BitbucketPRProxy":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class BitbucketFactory(ABC):
    """Builds and starts a BitbucketPRProxy from a config dict."""

    @classmethod
    @abstractmethod
    async def build(cls, cfg: dict) -> BitbucketPRProxy: ...


class ProviderError(Exception):
    pass


class ProviderNotFoundError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass
