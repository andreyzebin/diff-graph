from __future__ import annotations

from .base import (
    BitbucketDataProvider, JiraDataProvider,
    PullRequestData, FileDiff, FileContent,
    IssueData, JiraComment,
)


class BitbucketOverrides:
    def __init__(self, data: dict):
        self._pull_request = data.get("pull_request")
        self._files: dict[str, str] = {}
        for f in data.get("files", []):
            self._files[f["path"]] = f["content"]

    @property
    def pull_request(self):
        return self._pull_request

    def find_file(self, path: str) -> FileContent | None:
        if path in self._files:
            return FileContent(path=path, content=self._files[path])
        return None


class OverlayBitbucketProvider(BitbucketDataProvider):
    """Decorator: override specific data on top of a base provider."""

    def __init__(self, base: BitbucketDataProvider, overrides: BitbucketOverrides):
        self.base = base
        self.overrides = overrides

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequestData:
        if self.overrides.pull_request is not None:
            return self.overrides.pull_request
        return await self.base.get_pull_request(project, repo, pr_id)

    async def get_diff(self, project: str, repo: str, pr_id: int) -> list[FileDiff]:
        return await self.base.get_diff(project, repo, pr_id)

    async def get_file(self, project: str, repo: str, path: str, ref: str) -> FileContent | None:
        override = self.overrides.find_file(path)
        if override is not None:
            return override
        return await self.base.get_file(project, repo, path, ref)

    @property
    def current_pr_id(self) -> int:
        return self.base.current_pr_id


class JiraOverrides:
    def __init__(self, data: dict):
        self._issue = data.get("issue")
        self._comments = data.get("comments")

    @property
    def issue(self):
        return self._issue

    @property
    def comments(self):
        return self._comments


class OverlayJiraProvider(JiraDataProvider):
    def __init__(self, base: JiraDataProvider, overrides: JiraOverrides):
        self.base = base
        self.overrides = overrides

    async def get_issue(self, issue_key: str) -> IssueData:
        if self.overrides.issue is not None:
            return self.overrides.issue
        return await self.base.get_issue(issue_key)

    async def get_comments(self, issue_key: str) -> list[JiraComment]:
        if self.overrides.comments is not None:
            return self.overrides.comments
        return await self.base.get_comments(issue_key)
