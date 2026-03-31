from __future__ import annotations

import asyncio
import os

from atlassian import Bitbucket

from .base import (
    AgentPRViewFactory, AgentPRView, CommentAnchor, CommentThread, ReviewStatus,
    ProviderError,
)


class RealBitbucketPRProxy(AgentPRView):
    """
    Verification proxy for a real Bitbucket Server PR.

    Fetches comments and review status from the real API, filtered to the
    configured agent account.  close() declines the PR.
    """

    def __init__(
        self,
        client: Bitbucket,
        project: str,
        repo: str,
        _pr_id: int,
        agent_username: str,
    ):
        self._client = client
        self._project = project
        self._repo = repo
        self._pr_id = _pr_id
        self._agent_username = agent_username

    @property
    def pr_id(self) -> int:
        return self._pr_id

    # ── internal helpers ───────────────────────────────────────────────

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous atlassian-client call in a thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── AgentPRView interface ──────────────────────────────────────────

    async def close(self) -> None:
        pr = await self._run(
            self._client.get_pull_request, self._project, self._repo, self._pr_id
        )
        version = (pr or {}).get("version", 0)
        await self._run(
            self._client.decline_pull_request,
            self._project, self._repo, self._pr_id, version,
        )

    async def get_comments(self) -> list[CommentThread]:
        """Return comments posted by the agent account only."""
        url = self._client._url_pull_request_comments(
            self._project, self._repo, self._pr_id
        )
        raw = await self._run(lambda: list(self._client._get_paged(url)))
        comments = []
        for item in (raw or []):
            author = item.get("author", {})
            if author.get("slug") != self._agent_username:
                continue
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

    async def add_reviewer(self, username: str) -> None:
        """Add *username* as a reviewer, triggering any configured Bitbucket webhooks."""
        url = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/participants"
        )
        payload = {"user": {"name": username}, "role": "REVIEWER"}
        await self._run(self._client.post, url, data=payload)

    async def get_review_status(self) -> ReviewStatus | None:
        """Return the review status set by the agent account, or None."""
        url = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/participants"
        )
        data = await self._run(self._client.get, url)
        for p in (data or {}).get("values", []):
            user = p.get("user", {})
            if user.get("slug") != self._agent_username:
                continue
            status = p.get("status", "")
            if status in ("APPROVED", "NEEDS_WORK"):
                return ReviewStatus(status=status)
        return None


class RealBitbucketFactory(AgentPRViewFactory):
    """
    Builds a RealBitbucketPRProxy from config.

    Config keys:
      connection:
        base_url:      str
        project:       str
        repo:          str
        agent_account: str   (slug of the agent Bitbucket account)
        auth.env:      env var name holding the bearer token (default: BITBUCKET_TOKEN)
      pull_request:
        from_branch:   str
        to_branch:     str
        title:         str  (optional)
        description:   str  (optional)
    """

    @classmethod
    async def build(cls, cfg: dict) -> AgentPRView:
        conn = cfg["connection"]
        pr_cfg = cfg.get("pull_request", {})

        base_url = conn["base_url"].rstrip("/")
        project = conn["project"]
        repo = conn["repo"]
        token = os.environ.get(conn.get("auth", {}).get("env", "BITBUCKET_TOKEN"), "")
        agent_username = conn.get("agent_account", "")
        verify_ssl = cfg.get("verify_ssl", True)
        ssl_cfg = conn.get("ssl", {})

        client = Bitbucket(url=base_url, token=token, verify_ssl=verify_ssl)

        # Mutual TLS: client certificate (PEM) and/or custom CA bundle
        if ssl_cfg.get("ca_cert"):
            client._session.verify = ssl_cfg["ca_cert"]
        if ssl_cfg.get("client_cert"):
            key = ssl_cfg.get("client_key")
            client._session.cert = (ssl_cfg["client_cert"], key) if key else ssl_cfg["client_cert"]

        payload = {
            "title": pr_cfg.get("title", "[BENCHMARK]"),
            "description": pr_cfg.get("description", "Auto-created by benchmark"),
            "state": "OPEN",
            "fromRef": {"id": f"refs/heads/{pr_cfg['from_branch']}"},
            "toRef": {"id": f"refs/heads/{pr_cfg['to_branch']}"},
            "reviewers": [],
        }

        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.create_pull_request(project, repo, payload),
            )
        except Exception as exc:
            raise ProviderError(f"Failed to create PR: {exc}") from exc

        if not resp or "id" not in resp:
            raise ProviderError(f"Unexpected response when creating PR: {resp!r}")

        return RealBitbucketPRProxy(client, project, repo, resp["id"], agent_username)
