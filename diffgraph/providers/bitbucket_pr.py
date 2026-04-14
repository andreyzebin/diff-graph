"""
Bitbucket Server PR provider — REST API for PR metadata and comments.

Auth: Bearer token via HTTP header (works on all platforms).
"""
from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .base import PRProvider, PRMeta

log = logging.getLogger(__name__)


class BitbucketPRProvider(PRProvider):

    def __init__(
        self,
        token: str = "",
        ca_bundle: str = "",
        client_cert: str = "",
    ):
        self.token = (
            token
            or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN", "")
            or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN", "")
        )
        self.ca_bundle = ca_bundle or os.environ.get("REQUESTS_CA_BUNDLE", "")
        self.client_cert = (
            client_cert
            or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT", "")
            or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT", "")
        )

    def get_pr_meta(self, pr_url: str) -> PRMeta:
        server_url, project, repo, pr_id = _parse_pr_url(pr_url)
        endpoint = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}"
        )
        pr = self._api_get(endpoint)
        return PRMeta(
            title=pr.get("title", ""),
            description=pr.get("description", "") or "",
            author=pr.get("author", {}).get("user", {}).get("displayName", ""),
            from_branch=pr["fromRef"]["displayId"],
            to_branch=pr["toRef"]["displayId"],
            from_sha=pr["fromRef"]["latestCommit"],
            to_sha=pr["toRef"]["latestCommit"],
            pr_id=pr_id,
            extra={
                "base_ref": pr["toRef"]["latestCommit"],
                "source_ref": pr["fromRef"]["latestCommit"],
            },
        )

    def get_comments(self, pr_url: str) -> list[dict]:
        server_url, project, repo, pr_id = _parse_pr_url(pr_url)
        endpoint = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}/activities?limit=500"
        )
        try:
            data = self._api_get(endpoint)
        except Exception as exc:
            log.warning("failed to fetch comments: %s", exc)
            return []

        comments = []
        for activity in data.get("values", []):
            if activity.get("action") != "COMMENTED":
                continue
            c = activity.get("comment", {})
            if not c.get("id"):
                continue
            anchor = c.get("anchor") or activity.get("commentAnchor") or {}
            resolved = c.get("state") == "RESOLVED" or c.get("resolved", False)
            comments.append({
                "id": c["id"],
                "author": c.get("author", {}).get("slug", ""),
                "text": c.get("text", "")[:100],
                "file": anchor.get("path", ""),
                "line": anchor.get("line", 0),
                "resolved": resolved,
            })
        return comments

    def post_comment(self, pr_url: str, file: str, line: int,
                     text: str, severity: str = "NORMAL",
                     changed_lines: dict | None = None) -> None:
        server_url, project, repo, pr_id = _parse_pr_url(pr_url)
        endpoint = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}/comments"
        )
        payload: dict = {"text": text, "severity": severity}
        if file and line:
            anchor = {"path": file, "line": line, "lineType": "ADDED", "fileType": "TO"}
            if changed_lines and file in changed_lines:
                cl = changed_lines[file]
                if line not in cl and cl:
                    anchor["line"] = min(cl, key=lambda x: abs(x - line))
            payload["anchor"] = anchor
        self._api_post(endpoint, payload)

    def reply_to_comment(self, pr_url: str, comment_id: int, text: str) -> None:
        server_url, project, repo, pr_id = _parse_pr_url(pr_url)
        endpoint = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}/comments"
        )
        self._api_post(endpoint, {"text": text, "parent": {"id": comment_id}})

    def resolve_comment(self, pr_url: str, comment_id: int) -> None:
        server_url, project, repo, pr_id = _parse_pr_url(pr_url)
        endpoint = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}/comments/{comment_id}"
        )
        # Get current version first
        try:
            c = self._api_get(endpoint)
            version = c.get("version", 0)
        except Exception:
            version = 0
        self._api_put(endpoint, {"state": "RESOLVED", "version": version})

    def clone_url(self, pr_url: str) -> str:
        server_url, project, repo, _ = _parse_pr_url(pr_url)
        parsed = urlparse(server_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}/scm/{project.lower()}/{repo}.git"

    # ── HTTP helpers ──────────────────────────────────────────

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self.ca_bundle:
            ctx.load_verify_locations(self.ca_bundle)
        if self.client_cert:
            ctx.load_cert_chain(self.client_cert)
        return ctx

    def _api_get(self, url: str) -> dict:
        req = Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urlopen(req, context=self._ssl_context(), timeout=30) as resp:
            return json.loads(resp.read())

    def _api_post(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(req, context=self._ssl_context(), timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise HTTPError(e.url, e.code, body, e.headers, None) from None

    def _api_put(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = Request(url, data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(req, context=self._ssl_context(), timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise HTTPError(e.url, e.code, body, e.headers, None) from None


def _parse_pr_url(pr_url: str) -> tuple[str, str, str, int]:
    """Parse Bitbucket PR URL → (server_url, project, repo, pr_id)."""
    from diffgraph.bitbucket import parse_pr_url
    return parse_pr_url(pr_url)
