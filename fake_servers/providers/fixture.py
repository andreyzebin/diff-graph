from __future__ import annotations

from .base import (
    Author, PullRequestData, DiffHunk, FileDiff, FileContent,
    CommentAnchor, IssueData, JiraComment,
    BitbucketDataProvider, JiraDataProvider,
    ProviderNotFoundError,
)


class FixtureBitbucketProvider(BitbucketDataProvider):
    """Reads data from YAML scenario input.bitbucket.data section."""

    def __init__(self, data: dict):
        self._data = data

    def _build_pr(self) -> PullRequestData:
        d = self._data["pull_request"]
        author_d = d["author"]
        return PullRequestData(
            id=d["id"],
            title=d["title"],
            description=d.get("description", ""),
            author=Author(
                name=author_d["name"],
                display_name=author_d["display_name"],
            ),
            from_branch=d["from_branch"],
            to_branch=d["to_branch"],
            status=d["status"],
            head_commit=d["head_commit"],
        )

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequestData:
        if "pull_request" not in self._data:
            raise ProviderNotFoundError(f"No pull_request in fixture data")
        return self._build_pr()

    async def get_diff(self, project: str, repo: str, pr_id: int) -> list[FileDiff]:
        diffs = []
        for fd in self._data.get("diff", []):
            hunks = [
                DiffHunk(
                    old_start=h["old_start"],
                    new_start=h["new_start"],
                    lines=h["lines"],
                )
                for h in fd.get("hunks", [])
            ]
            diffs.append(FileDiff(
                path=fd["path"],
                change_type=fd.get("change_type", "MODIFY"),
                hunks=hunks,
            ))
        return diffs

    async def get_file(self, project: str, repo: str, path: str, ref: str) -> FileContent | None:
        for f in self._data.get("codebase_context", []):
            if f["path"] == path:
                return FileContent(path=f["path"], content=f["content"])
        return None

    @property
    def current_pr_id(self) -> int:
        return self._data["pull_request"]["id"]


class FixtureJiraProvider(JiraDataProvider):
    """Reads data from YAML scenario input.jira.data section."""

    def __init__(self, data: dict):
        self._data = data

    async def get_issue(self, issue_key: str) -> IssueData:
        issue = self._data.get("issue")
        if issue is None:
            raise ProviderNotFoundError(f"No issue in fixture data")
        return IssueData(
            key=issue["key"],
            summary=issue["summary"],
            description=issue.get("description", ""),
            issuetype=issue.get("issuetype", "Task"),
            status=issue.get("status", "Open"),
            labels=issue.get("labels", []),
        )

    async def get_comments(self, issue_key: str) -> list[JiraComment]:
        comments = []
        for i, c in enumerate(self._data.get("comments", [])):
            comments.append(JiraComment(
                id=c.get("id", i + 1),
                body=c.get("body", ""),
                author=c.get("author", "unknown"),
            ))
        return comments
