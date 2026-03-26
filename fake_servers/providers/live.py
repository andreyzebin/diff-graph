from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Any

import httpx

from .base import (
    Author, PullRequestData, DiffHunk, FileDiff, FileContent,
    IssueData, JiraComment,
    BitbucketDataProvider, JiraDataProvider,
    ProviderError, ProviderNotFoundError, ProviderAuthError,
)


class LiveBitbucketProvider(BitbucketDataProvider):
    """Reads from a real Bitbucket Server via REST API."""

    def __init__(self, connection: dict, pull_request_cfg: dict | None = None):
        self._conn = connection
        self._pr_cfg = pull_request_cfg or {}
        self._base_url = connection["base_url"].rstrip("/")
        auth = connection.get("auth", {})
        token = os.environ.get(auth.get("env", "BITBUCKET_TOKEN"), "")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._project = connection.get("project", "")
        self._repo = connection.get("repo", "")
        self._current_pr_id: int | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequestData:
        url = f"{self._base_url}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
        async with self._client() as c:
            resp = await c.get(url)
        if resp.status_code == 404:
            raise ProviderNotFoundError(f"PR {pr_id} not found")
        if resp.status_code == 401:
            raise ProviderAuthError("Auth error")
        if resp.status_code >= 400:
            raise ProviderError(f"Bitbucket error: {resp.status_code}")
        data = resp.json()
        return PullRequestData(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            author=Author(
                name=data["author"]["user"]["name"],
                display_name=data["author"]["user"]["displayName"],
            ),
            from_branch=data["fromRef"]["displayId"],
            to_branch=data["toRef"]["displayId"],
            status=data["state"],
            head_commit=data["fromRef"]["latestCommit"],
        )

    async def get_diff(self, project: str, repo: str, pr_id: int) -> list[FileDiff]:
        url = f"{self._base_url}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests/{pr_id}/diff"
        async with self._client() as c:
            resp = await c.get(url)
        if resp.status_code >= 400:
            raise ProviderError(f"Diff error: {resp.status_code}")
        data = resp.json()
        diffs = []
        for fd in data.get("diffs", []):
            hunks = []
            for h in fd.get("hunks", []):
                lines = []
                for seg in h.get("segments", []):
                    prefix = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}.get(seg["type"], " ")
                    for l in seg.get("lines", []):
                        lines.append(f"{prefix}{l['line']}")
                hunks.append(DiffHunk(
                    old_start=h.get("sourceLine", 0),
                    new_start=h.get("destinationLine", 0),
                    lines=lines,
                ))
            diffs.append(FileDiff(
                path=fd.get("destination", {}).get("toString", fd.get("source", {}).get("toString", "")),
                change_type=fd.get("fileType", "MODIFY"),
                hunks=hunks,
            ))
        return diffs

    async def get_file(self, project: str, repo: str, path: str, ref: str) -> FileContent | None:
        url = f"{self._base_url}/rest/api/1.0/projects/{project}/repos/{repo}/browse/{path}"
        async with self._client() as c:
            resp = await c.get(url, params={"at": ref})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(f"Browse error: {resp.status_code}")
        data = resp.json()
        lines = [l["text"] for l in data.get("lines", [])]
        return FileContent(path=path, content="\n".join(lines))

    @asynccontextmanager
    async def pr_lifecycle(self, scenario) -> AsyncIterator[None]:
        """Create PR before scenario, decline it after."""
        pr_id = await self._create_pr()
        self._current_pr_id = pr_id
        try:
            yield
        finally:
            await self._decline_pr(pr_id)
            self._current_pr_id = None

    async def _create_pr(self) -> int:
        url = f"{self._base_url}/rest/api/1.0/projects/{self._project}/repos/{self._repo}/pull-requests"
        payload = {
            "title": self._pr_cfg.get("title", "[BENCHMARK]"),
            "description": self._pr_cfg.get("description", "Auto-created by benchmark"),
            "state": "OPEN",
            "fromRef": {"id": f"refs/heads/{self._pr_cfg['from_branch']}"},
            "toRef": {"id": f"refs/heads/{self._pr_cfg['to_branch']}"},
            "reviewers": [],
        }
        async with self._client() as c:
            resp = await c.post(url, json=payload)
        if resp.status_code not in (200, 201):
            raise ProviderError(f"Failed to create PR: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    async def _decline_pr(self, pr_id: int) -> None:
        url = f"{self._base_url}/rest/api/1.0/projects/{self._project}/repos/{self._repo}/pull-requests/{pr_id}/decline"
        async with self._client() as c:
            await c.post(url, json={})

    @property
    def current_pr_id(self) -> int:
        if self._current_pr_id is None:
            raise RuntimeError("No active PR — call within pr_lifecycle context")
        return self._current_pr_id


class LiveJiraProvider(JiraDataProvider):
    """Reads from a real Jira instance via REST API."""

    def __init__(self, connection: dict):
        self._base_url = connection["base_url"].rstrip("/")
        auth = connection.get("auth", {})
        token = os.environ.get(auth.get("env", "JIRA_TOKEN"), "")
        self._headers = {"Authorization": f"Bearer {token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    async def get_issue(self, issue_key: str) -> IssueData:
        url = f"{self._base_url}/rest/api/2/issue/{issue_key}"
        async with self._client() as c:
            resp = await c.get(url)
        if resp.status_code == 404:
            raise ProviderNotFoundError(f"Issue {issue_key} not found")
        if resp.status_code >= 400:
            raise ProviderError(f"Jira error: {resp.status_code}")
        data = resp.json()
        fields = data.get("fields", {})
        return IssueData(
            key=data["key"],
            summary=fields.get("summary", ""),
            description=fields.get("description", ""),
            issuetype=fields.get("issuetype", {}).get("name", "Task"),
            status=fields.get("status", {}).get("name", "Open"),
            labels=fields.get("labels", []),
        )

    async def get_comments(self, issue_key: str) -> list[JiraComment]:
        url = f"{self._base_url}/rest/api/2/issue/{issue_key}/comment"
        async with self._client() as c:
            resp = await c.get(url)
        if resp.status_code >= 400:
            raise ProviderError(f"Jira comments error: {resp.status_code}")
        data = resp.json()
        from datetime import datetime
        comments = []
        for c in data.get("comments", []):
            comments.append(JiraComment(
                id=int(c["id"]),
                body=c.get("body", ""),
                author=c.get("author", {}).get("displayName", "unknown"),
                created=datetime.fromisoformat(c["created"].replace("Z", "+00:00")),
            ))
        return comments
