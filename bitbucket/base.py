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

    @property
    def pr_url(self) -> str | None:
        """Full web URL of the pull request, or None if not available."""
        return None

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
    async def get_review_status(self, verdict_source: str = "api") -> ReviewStatus | None:
        """
        Return the review status submitted by the agent account, or ``None``.

        ``verdict_source`` selects the regulated channel through which the
        agent surfaces its verdict — part of the agent's output interface
        contract:
        - ``"api"`` (production): read from Bitbucket's participants
          endpoint. The agent must be a participant; if it's the PR author
          self-approve is typically blocked.
        - ``"comment"`` (bench-friendly): scan the agent's general comments
          for a ``[verdict:STATUS]`` marker. Useful when the bench creates
          PRs under the bot's own token and the API path can't be used.
        - ``"both"``: prefer the API value, fall back to comment marker.

        Implementation requirements:
        - Filter strictly by the configured agent account — statuses set by
          other participants must be ignored.
        - Only ``"APPROVED"`` and ``"NEEDS_WORK"`` are meaningful statuses;
          ``"UNAPPROVED"`` (the default) must be treated as absent and
          ``None`` returned.
        - Return ``None`` when the agent has not yet submitted a decision.
        """
        ...

    async def add_reviewer(self, username: str) -> None:
        """
        Add *username* as a reviewer on the pull request.

        Used by the webhook trigger strategy to notify the agent via
        Bitbucket's built-in reviewer webhook rather than an HTTP call.
        Implementations that do not support this may raise ``NotImplementedError``.
        """
        raise NotImplementedError

    async def add_comment(self, text: str, parent_id: int | None = None) -> int:
        """
        Post a general (non-inline) comment on the PR.

        Returns the new comment's id. Implementations that don't support
        this may raise NotImplementedError.

        Used by interaction scenarios that need to seed a thread before
        triggering the agent (/ask, /help, multi-turn conversations) and
        by the comment-based trigger which posts a "/command" message
        as the last seed_comment to fire `pr:comment:added`.
        """
        raise NotImplementedError

    async def get_all_comments(self) -> list[CommentThread]:
        """
        Return EVERY comment on the PR, regardless of author.

        Differs from get_comments() which filters to the agent account.
        Used by the reply-scoring judge: it needs to see the full thread
        the agent saw, including seed comments posted by the runner.
        """
        raise NotImplementedError

    async def get_diff(self) -> str:
        """
        Return the unified diff of the PR as a single string.

        Lets the judge ground its agent_warnings (wrong-location,
        contradicts-codebase) on the actual changed code. Implementations
        that don't support this may return "" or raise NotImplementedError.
        """
        raise NotImplementedError

    async def get_raw_file(self, path: str, ref: str = "") -> str:
        """
        Return the raw text content of a file at the given ref (commit/branch)
        in the PR's repo. Empty ref means the source branch tip.

        Used by the judge to read AGENTS.md (or similar project-convention
        docs) so methodology-gap / contradicts-codebase warnings can be
        verified rather than asserted from world knowledge alone.

        Implementations may return "" when the file doesn't exist instead
        of raising — best-effort reads keep the judge robust.
        """
        raise NotImplementedError

    async def __aenter__(self) -> "AgentPRView":
        return self

    async def __aexit__(self, *exc) -> None:
        # Defensive: a failure in close() must NOT poison the next
        # iteration. If decline returns 5xx or the network blips, log
        # and move on — the benchmark loop tolerates leftover PRs by
        # auto-declining stale BENCHMARK PRs at next session start.
        import logging
        log = logging.getLogger(__name__)
        try:
            await self.close()
        except Exception as e:
            log.warning("AgentPRView.close() failed (non-fatal): %s", e)


class AgentPRViewFactory(ABC):
    """Creates a PR in Bitbucket and returns an AgentPRView bound to it."""

    @classmethod
    @abstractmethod
    async def build(cls, cfg: dict) -> AgentPRView: ...


class ProviderError(Exception):
    pass
