"""
Abstract base classes for PR and repo providers.

PRProvider — platform API (Bitbucket, GitHub, GitLab)
RepoProvider — version control operations (git, etc.)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class PRMeta:
    title: str = ""
    description: str = ""
    author: str = ""
    from_branch: str = ""
    to_branch: str = ""
    from_sha: str = ""
    to_sha: str = ""
    pr_id: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class PRComment:
    id: int
    author: str
    text: str
    file_path: str = ""
    line: int = 0
    resolved: bool = False


class PRProvider(ABC):
    """Interface to a PR platform (Bitbucket, GitHub, GitLab)."""

    @abstractmethod
    def get_pr_meta(self, pr_url: str) -> PRMeta:
        """Fetch PR metadata."""

    @abstractmethod
    def get_comments(self, pr_url: str) -> list[dict]:
        """Fetch existing PR comments."""

    @abstractmethod
    def post_comment(self, pr_url: str, file: str, line: int,
                     text: str, severity: str = "NORMAL",
                     changed_lines: dict | None = None) -> None:
        """Post an inline comment to the PR."""

    @abstractmethod
    def reply_to_comment(self, pr_url: str, comment_id: int, text: str) -> None:
        """Reply to an existing comment thread."""

    @abstractmethod
    def resolve_comment(self, pr_url: str, comment_id: int) -> None:
        """Mark a comment as resolved."""

    def clone_url(self, pr_url: str) -> str:
        """Return the git clone URL for the PR's repository."""
        return ""


class RepoProvider(ABC):
    """Interface to version control operations."""

    @abstractmethod
    def clone(self, url: str, branch: str, dest: str) -> None:
        """Clone a repository."""

    @abstractmethod
    def fetch(self, repo_path: str, ref: str) -> None:
        """Fetch a specific ref."""

    @abstractmethod
    def diff(self, repo_path: str, base: str, source: str) -> str:
        """Compute diff between two refs."""

    @abstractmethod
    def log_oneline(self, repo_path: str, base: str, source: str) -> str:
        """Oneline commit log between two refs."""
