from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator


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
    status: str           # OPEN | MERGED | DECLINED
    head_commit: str


@dataclass
class DiffHunk:
    old_start: int
    new_start: int
    lines: list[str]


@dataclass
class FileDiff:
    path: str
    change_type: str      # MODIFY | ADD | DELETE | RENAME
    hunks: list[DiffHunk]


@dataclass
class FileContent:
    path: str
    content: str


@dataclass
class CommentAnchor:
    path: str
    line: int
    line_type: str        # ADDED | REMOVED | CONTEXT


@dataclass
class CommentThread:
    id: int
    text: str
    anchor: CommentAnchor | None   # None → general PR comment
    severity: str = "NORMAL"       # NORMAL | BLOCKER
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ReviewStatus:
    status: str           # APPROVED | NEEDS_WORK
    set_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CapturedOutput:
    """Everything the agent did during a scenario run. Passed to Judge."""
    comments: list[CommentThread] = field(default_factory=list)
    review_status: ReviewStatus | None = None

    @property
    def inline_comments(self) -> list[CommentThread]:
        return [c for c in self.comments if c.anchor is not None]

    @property
    def general_comments(self) -> list[CommentThread]:
        return [c for c in self.comments if c.anchor is None]


@dataclass
class IssueData:
    key: str
    summary: str
    description: str
    issuetype: str
    status: str
    labels: list[str] = field(default_factory=list)


@dataclass
class JiraComment:
    id: int
    body: str
    author: str
    created: datetime = field(default_factory=datetime.utcnow)


# ── READ interface ────────────────────────────────────────────────

class BitbucketDataProvider(ABC):

    @abstractmethod
    async def get_pull_request(
        self, project: str, repo: str, pr_id: int
    ) -> PullRequestData: ...

    @abstractmethod
    async def get_diff(
        self, project: str, repo: str, pr_id: int
    ) -> list[FileDiff]: ...

    @abstractmethod
    async def get_file(
        self, project: str, repo: str, path: str, ref: str
    ) -> FileContent | None:
        """Return None if file not found → Fake Server returns 404."""
        ...

    @asynccontextmanager
    async def pr_lifecycle(self, scenario) -> AsyncIterator[None]:
        """Context manager for PR lifecycle. No-op for fixture providers."""
        yield

    @property
    def current_pr_id(self) -> int:
        raise NotImplementedError


class BitbucketWriteSink(ABC):

    @abstractmethod
    async def add_comment(
        self, pr_id: int, text: str, anchor: CommentAnchor | None
    ) -> CommentThread: ...

    @abstractmethod
    async def set_review_status(
        self, pr_id: int, status: str
    ) -> ReviewStatus: ...

    @abstractmethod
    async def get_captured(self) -> CapturedOutput: ...

    @abstractmethod
    async def reset(self) -> None: ...


class JiraDataProvider(ABC):

    @abstractmethod
    async def get_issue(self, issue_key: str) -> IssueData: ...

    @abstractmethod
    async def get_comments(self, issue_key: str) -> list[JiraComment]: ...


# ── Exceptions ────────────────────────────────────────────────────

class ProviderError(Exception):
    """Provider error → Fake Server returns 502."""


class ProviderNotFoundError(ProviderError):
    """PR or repository not found."""


class ProviderAuthError(ProviderError):
    """Authentication error."""
