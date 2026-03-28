from __future__ import annotations

import os

import httpx

from .base import (
    BitbucketFactory, BitbucketPRProxy, CommentAnchor, CommentThread, ReviewStatus,
    ProviderError,
)


class RealBitbucketPRProxy(BitbucketPRProxy):
    """
    Verification proxy for a real Bitbucket Server PR.

    Fetches comments and review status from the real API.
    close() declines the PR and releases the HTTP session.
    """

    def __init__(
        self,
        base_url: str,
        headers: dict,
        project: str,
        repo: str,
        _pr_id: int,
    ):
        self._base_url = base_url
        self._headers = headers
        self._project = project
        self._repo = repo
        self._pr_id = _pr_id

    @property
    def pr_id(self) -> int:
        return self._pr_id

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    def _pr_base(self) -> str:
        return (
            f"{self._base_url}/rest/api/1.0"
            f"/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}"
        )

    async def close(self) -> None:
        async with self._client() as c:
            await c.post(f"{self._pr_base()}/decline", json={})

    async def get_comments(self) -> list[CommentThread]:
        async with self._client() as c:
            resp = await c.get(f"{self._pr_base()}/comments")
        resp.raise_for_status()
        comments = []
        for item in resp.json().get("values", []):
            anchor_data = item.get("anchor")
            anchor = None
            if anchor_data:
                anchor = CommentAnchor(
                    path=anchor_data.get("path", ""),
                    line=anchor_data.get("line", 0),
                    line_type=anchor_data.get("lineType", "ADDED"),
                )
            comments.append(CommentThread(
                id=item["id"],
                text=item.get("text", ""),
                anchor=anchor,
                severity=item.get("severity", "NORMAL"),
            ))
        return comments

    async def get_review_status(self) -> ReviewStatus | None:
        async with self._client() as c:
            resp = await c.get(f"{self._pr_base()}/participants")
        resp.raise_for_status()
        for p in resp.json().get("values", []):
            status = p.get("status", "")
            if status in ("APPROVED", "NEEDS_WORK"):
                return ReviewStatus(status=status)
        return None


class RealBitbucketFactory(BitbucketFactory):
    """
    Builds a RealBitbucketPRProxy from config.

    Config keys:
      connection:
        base_url:  str
        project:   str
        repo:      str
        auth.env:  env var name holding the bearer token (default: BITBUCKET_TOKEN)
      pull_request:
        from_branch: str
        to_branch:   str
        title:       str  (optional)
        description: str  (optional)
    """

    @classmethod
    async def build(cls, cfg: dict) -> BitbucketPRProxy:
        conn = cfg["connection"]
        pr_cfg = cfg.get("pull_request", {})

        base_url = conn["base_url"].rstrip("/")
        project = conn["project"]
        repo = conn["repo"]
        token = os.environ.get(conn.get("auth", {}).get("env", "BITBUCKET_TOKEN"), "")
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "title": pr_cfg.get("title", "[BENCHMARK]"),
            "description": pr_cfg.get("description", "Auto-created by benchmark"),
            "state": "OPEN",
            "fromRef": {"id": f"refs/heads/{pr_cfg['from_branch']}"},
            "toRef": {"id": f"refs/heads/{pr_cfg['to_branch']}"},
            "reviewers": [],
        }
        url = f"{base_url}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests"
        async with httpx.AsyncClient(headers=headers, timeout=30) as c:
            resp = await c.post(url, json=payload)
        if resp.status_code not in (200, 201):
            raise ProviderError(f"Failed to create PR: {resp.status_code} {resp.text}")

        pr_id = resp.json()["id"]
        return RealBitbucketPRProxy(base_url, headers, project, repo, pr_id)
