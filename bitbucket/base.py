from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


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


class AgentPRView(ABC):
    """
    Read-only view of the agent's activity on a single benchmark PR,
    plus lifecycle control (close).

    An implementation wraps one pull-request that was created for the
    benchmark run and returns only the outputs that belong to the
    configured agent account — every method must filter out activity
    from humans or other bots.
    """

    @property
    @abstractmethod
    def pr_id(self) -> int:
        """Identifier of the pull request this view is bound to."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """
        Decline the pull request and release any held resources (e.g. HTTP
        sessions).

        Must be idempotent: calling it more than once must not raise.
        """
        ...

    @abstractmethod
    async def get_comments(self) -> list[CommentThread]:
        """
        Return all comments posted by the agent account on this PR.

        Implementation requirements:
        - Filter strictly by the configured agent account — comments from
          humans, reviewers, or other bots must be excluded.
        - Inline comments must have a populated ``CommentThread.anchor``
          (file path, line number, line type).
        - General PR-level comments must have ``anchor = None``.
        - Return an empty list when the agent has posted no comments yet;
          never raise on an empty result.
        """
        ...

    @abstractmethod
    async def get_review_status(self) -> ReviewStatus | None:
        """
        Return the review status submitted by the agent account, or ``None``.

        Implementation requirements:
        - Filter strictly by the configured agent account — statuses set by
          other participants must be ignored.
        - Only ``"APPROVED"`` and ``"NEEDS_WORK"`` are meaningful statuses;
          ``"UNAPPROVED"`` (the default) must be treated as absent and
          ``None`` returned.
        - Return ``None`` when the agent has not yet submitted a decision.
        """
        ...

    async def __aenter__(self) -> "AgentPRView":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class AgentPRViewFactory(ABC):
    """Creates a PR in Bitbucket and returns an AgentPRView bound to it."""

    @classmethod
    @abstractmethod
    async def build(cls, cfg: dict) -> AgentPRView: ...


class ProviderError(Exception):
    pass
