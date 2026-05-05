from __future__ import annotations

import asyncio
import logging
import os
import time

from atlassian import Bitbucket
import requests.exceptions as _re

log = logging.getLogger(__name__)

from .base import (
    AgentPRViewFactory, AgentPRView, CommentAnchor, CommentThread, ReviewStatus,
    ProviderError,
)


# Transient network/HTTP errors that should be retried — corp Bitbucket
# instances over VPN occasionally drop a single request and a tight retry
# brings the next one back. We deliberately don't retry HTTP 4xx/5xx that
# come back as a real Response, only network-layer failures.
_TRANSIENT = (
    _re.ConnectTimeout,
    _re.ReadTimeout,
    _re.ConnectionError,
)


def _retry(fn, *, attempts: int = 3, delay: float = 2.0):
    """Call fn() with up to *attempts* tries, sleeping *delay* between them.

    Re-raises the last exception if every attempt fails.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except _TRANSIENT as exc:
            last = exc
            log.warning("transient bitbucket error (try %d/%d): %s",
                        i + 1, attempts, exc)
            if i + 1 < attempts:
                time.sleep(delay)
    assert last is not None
    raise last


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
        base_url: str = "",
    ):
        self._client = client
        self._project = project
        self._repo = repo
        self._pr_id = _pr_id
        self._agent_username = agent_username
        self._base_url = base_url.rstrip("/")

    @property
    def pr_id(self) -> int:
        return self._pr_id

    @property
    def pr_url(self) -> str | None:
        if not self._base_url:
            return None
        return (
            f"{self._base_url}/projects/{self._project}"
            f"/repos/{self._repo}/pull-requests/{self._pr_id}"
        )

    # ── internal helpers ───────────────────────────────────────────────

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous atlassian-client call in a thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── AgentPRView interface ──────────────────────────────────────────

    async def close(self) -> None:
        log.info("declining PR #%d (%s/%s)", self._pr_id, self._project, self._repo)
        pr = await self._run(
            lambda: _retry(lambda: self._client.get_pull_request(
                self._project, self._repo, self._pr_id
            ))
        )
        version = (pr or {}).get("version", 0)
        # atlassian-python-api's decline_pull_request silently 403s on Bitbucket
        # Server when the request lacks `X-Atlassian-Token: no-check` (XSRF
        # protection). Hit the REST endpoint directly and check the response so
        # a failure becomes a real exception, not a phantom "PR auto-closed".
        await self._run(self._decline_via_rest, self._pr_id, version)
        log.info("PR #%d declined", self._pr_id)

    def _decline_via_rest(self, pr_id: int, version: int) -> None:
        # Go through atlassian-python-api's request layer so verify/cert
        # configured on the client (corp CA + mTLS) are applied. Calling
        # self._client._session.post directly skipped both, so the request
        # silently failed on corporate Bitbucket Server installs.
        path = (
            f"rest/api/1.0/projects/{self._project}"
            f"/repos/{self._repo}/pull-requests/{pr_id}/decline"
        )
        resp = self._client.post(
            path,
            params={"version": version},
            headers={"X-Atlassian-Token": "no-check"},
            advanced_mode=True,   # return raw Response so we can check status
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"decline PR #{pr_id} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )

    async def get_comments(self) -> list[CommentThread]:
        """Return comments posted by the agent account only."""
        return [c for c in await self._fetch_all_comments()
                if c._author == self._agent_username]

    async def add_reviewer(self, username: str) -> None:
        """Add *username* as a reviewer, triggering any configured Bitbucket webhooks."""
        url = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/participants"
        )
        payload = {"user": {"name": username}, "role": "REVIEWER"}
        await self._run(self._client.post, url, data=payload)

    async def add_comment(self, text: str, parent_id: int | None = None) -> int:
        """
        Post a general PR comment. Fires `pr:comment:added` webhook.

        Returns the new comment id (for follow-up replies in a thread).
        Goes through the SDK request layer so verify/cert and the
        XSRF-noop header are applied — same pattern as _decline_via_rest.

        Pass body via `json=` so the SDK sets Content-Type: application/json;
        Bitbucket rejects this endpoint with HTTP 415 otherwise.
        """
        path = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/comments"
        )
        body: dict = {"text": text}
        if parent_id is not None:
            body["parent"] = {"id": parent_id}
        resp = await self._run(
            self._client.post, path,
            json=body,
            headers={"X-Atlassian-Token": "no-check"},
            advanced_mode=True,
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"add_comment failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        try:
            return int(resp.json().get("id", 0))
        except (ValueError, AttributeError):
            return 0

    async def get_all_comments(self) -> list[CommentThread]:
        """Return every comment on the PR, regardless of author."""
        return await self._fetch_all_comments()

    async def get_diff(self) -> str:
        """Fetch the PR's unified diff. Returns "" on transient failure."""
        path = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/diff"
        )
        def _do() -> str:
            try:
                resp = _retry(lambda: self._client.get(path, advanced_mode=True))
                if resp.status_code >= 400:
                    log.warning("get_diff #%d HTTP %d", self._pr_id, resp.status_code)
                    return ""
                return resp.text or ""
            except Exception as exc:
                log.warning("get_diff #%d failed: %s", self._pr_id, exc)
                return ""
        return await self._run(_do)

    async def get_raw_file(self, file_path: str, ref: str = "") -> str:
        """Fetch raw file content at *ref* (default: source branch tip).

        Returns "" when the file doesn't exist or the request fails — the
        judge uses this best-effort, so a missing AGENTS.md should not
        crash the evaluation.
        """
        path = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}/raw/{file_path}"
        )
        def _do() -> str:
            try:
                params = {"at": f"refs/heads/{ref}"} if ref else None
                resp = _retry(lambda: self._client.get(
                    path, params=params, advanced_mode=True
                ))
                if resp.status_code == 404:
                    return ""
                if resp.status_code >= 400:
                    log.warning("get_raw_file %s HTTP %d", file_path, resp.status_code)
                    return ""
                return resp.text or ""
            except Exception as exc:
                log.warning("get_raw_file %s failed: %s", file_path, exc)
                return ""
        return await self._run(_do)

    async def _fetch_all_comments(self) -> list[CommentThread]:
        """Walk PR activity, flatten root + nested replies into one list.

        Bitbucket Server's activity API returns each top-level COMMENTED
        activity as { "comment": { ..., "comments": [reply, ...] } } — the
        replies are nested INSIDE the root comment's `comments` field, not
        as separate activities. Earlier versions only consumed the root,
        so reply_to_comment() outputs were invisible to the bench.
        """
        activities = await self._run(
            lambda: _retry(lambda: list(self._client.get_pull_requests_activities(
                self._project, self._repo, self._pr_id
            )))
        )
        roots = [
            a["comment"] for a in (activities or [])
            if a.get("action") == "COMMENTED" and "comment" in a
        ]
        flat: list[CommentThread] = []
        stack = list(roots)
        while stack:
            item = stack.pop()
            anchor_data = item.get("anchor")
            anchor = None
            if anchor_data:
                anchor = CommentAnchor(
                    path=anchor_data.get("path", ""),
                    line=anchor_data.get("line", 0),
                    line_type=anchor_data.get("lineType", "ADDED"),
                )
            ct = CommentThread(
                id=item.get("id", 0),
                text=item.get("text", ""),
                anchor=anchor,
                severity=item.get("severity", "NORMAL"),
            )
            # Stash author on the dataclass instance for downstream filter.
            # CommentThread doesn't carry author publicly to keep the API
            # narrow; this is bench-internal state.
            ct._author = item.get("author", {}).get("slug", "")
            flat.append(ct)
            for child in item.get("comments", []) or []:
                stack.append(child)
        return flat

    async def get_review_status(self, verdict_source: str = "api") -> ReviewStatus | None:
        """Return the review status set by the agent account, or None.

        See AgentPRView.get_review_status for the verdict_source contract.
        """
        mode = (verdict_source or "api").strip().lower()
        api_status = await self._read_status_from_api() if mode in ("api", "both") else None
        if api_status is not None:
            return api_status
        if mode in ("comment", "both"):
            return await self._read_status_from_comment_marker()
        return None

    async def _read_status_from_api(self) -> ReviewStatus | None:
        url = (
            f"rest/api/1.0/projects/{self._project}/repos/{self._repo}"
            f"/pull-requests/{self._pr_id}/participants"
        )
        data = await self._run(lambda: _retry(lambda: self._client.get(url)))
        for p in (data or {}).get("values", []):
            user = p.get("user", {})
            if user.get("slug") != self._agent_username:
                continue
            status = p.get("status", "")
            if status in ("APPROVED", "NEEDS_WORK"):
                return ReviewStatus(status=status)
        return None

    async def _read_status_from_comment_marker(self) -> ReviewStatus | None:
        """Scan the agent's general comments for a ``[verdict:STATUS]`` marker.

        The agent posts these when running with --verdict-mode=comment so a
        bot that opened the PR (and thus can't self-approve via the API)
        can still surface a verdict the bench's judge can read.
        """
        import re as _re
        comments = await self._fetch_all_comments()
        marker = _re.compile(r"\[verdict:(APPROVED|NEEDS_WORK|UNAPPROVED)\]", _re.IGNORECASE)
        # Newest-first: a later comment supersedes earlier ones.
        for c in reversed(comments):
            if c.anchor is not None:
                continue  # only top-level / general comments carry the verdict
            if c._author != self._agent_username:
                continue
            m = marker.search(c.text or "")
            if not m:
                continue
            status = m.group(1).upper()
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

        # Bitbucket allows only one open PR per (from, to) branch pair. Stale
        # PRs from prior interrupted runs (process killed, network blip, etc.)
        # block fresh PR creation. Decline any existing open `[BENCHMARK]` PR
        # on the same branch pair before creating ours.
        await loop.run_in_executor(
            None,
            lambda: cls._decline_stale_benchmark_prs(
                client, project, repo,
                pr_cfg["from_branch"], pr_cfg["to_branch"],
            ),
        )

        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.create_pull_request(project, repo, payload),
            )
        except Exception as exc:
            raise ProviderError(f"Failed to create PR: {exc}") from exc

        if not resp or "id" not in resp:
            raise ProviderError(f"Unexpected response when creating PR: {resp!r}")

        return RealBitbucketPRProxy(client, project, repo, resp["id"], agent_username, base_url)

    @staticmethod
    def _decline_stale_benchmark_prs(client: Bitbucket, project: str, repo: str,
                                     from_branch: str, to_branch: str) -> None:
        """Decline any open `[BENCHMARK]` PR on the (from, to) branch pair.

        Best-effort: a network blip or unexpected response shape is logged but
        not raised — the subsequent create_pull_request call is the real
        contract, and it will surface "duplicate PR" if cleanup didn't help.
        """
        path = f"rest/api/1.0/projects/{project}/repos/{repo}/pull-requests"
        try:
            resp = client.get(
                path,
                params={
                    "state": "OPEN",
                    "at": f"refs/heads/{from_branch}",
                    "direction": "OUTGOING",
                    "limit": 50,
                },
                advanced_mode=True,
            )
            if resp.status_code >= 400:
                log.warning("list open PRs failed: HTTP %d %s",
                            resp.status_code, resp.text[:200])
                return
            data = resp.json() or {}
        except Exception as exc:
            log.warning("list open PRs failed: %s", exc)
            return

        target_ref = f"refs/heads/{to_branch}"
        for pr in (data.get("values") or []):
            if (pr.get("toRef") or {}).get("id") != target_ref:
                continue
            title = pr.get("title", "") or ""
            if "[BENCHMARK]" not in title:
                # Don't touch unrelated open PRs from real users.
                continue
            pr_id = pr.get("id")
            version = pr.get("version", 0)
            try:
                dr = client.post(
                    f"{path}/{pr_id}/decline",
                    params={"version": version},
                    headers={"X-Atlassian-Token": "no-check"},
                    advanced_mode=True,
                )
                if dr.status_code >= 400:
                    log.warning("decline stale PR #%s failed: HTTP %d %s",
                                pr_id, dr.status_code, dr.text[:200])
                else:
                    log.info("declined stale BENCHMARK PR #%s on %s -> %s",
                             pr_id, from_branch, to_branch)
            except Exception as exc:
                log.warning("decline stale PR #%s raised: %s", pr_id, exc)
