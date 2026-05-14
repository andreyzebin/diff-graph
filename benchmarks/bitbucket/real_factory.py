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
# brings the next one back. SSLError is included because Bitbucket Server
# under load returns `SSL: UNEXPECTED_EOF_WHILE_READING` mid-handshake,
# which `requests` surfaces as SSLError, not ConnectionError. We
# deliberately don't retry HTTP 4xx/5xx that come back as a real
# Response, only network-layer failures.
_TRANSIENT = (
    _re.ConnectTimeout,
    _re.ReadTimeout,
    _re.ConnectionError,
    _re.SSLError,
)


# Tunable defaults for `_retry`. Picked up from environment so callers
# don't have to thread the values through every call site:
#   BENCH_BB_RETRY_ATTEMPTS=N       — total tries (default 5)
#   BENCH_BB_RETRY_BASE_DELAY=SEC   — first-retry sleep (default 1.0s)
#   BENCH_BB_RETRY_MAX_DELAY=SEC    — cap per-retry sleep (default 30s)
# Backoff is exponential: delay_i = min(base * 2**i, max). Aggressive
# bench runs against an overloaded Bitbucket Server need this; gentle
# runs almost never trip it but the same defaults are safe.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


_DEFAULT_ATTEMPTS = _env_int("BENCH_BB_RETRY_ATTEMPTS", 5)
_DEFAULT_BASE_DELAY = _env_float("BENCH_BB_RETRY_BASE_DELAY", 1.0)
_DEFAULT_MAX_DELAY = _env_float("BENCH_BB_RETRY_MAX_DELAY", 30.0)


def _retry(fn, *, attempts: int | None = None, delay: float | None = None,
           max_delay: float | None = None):
    """Call fn() with exponential backoff on transient errors.

    `attempts`     — total number of tries (default from BENCH_BB_RETRY_ATTEMPTS).
    `delay`        — sleep before the first retry (default from BENCH_BB_RETRY_BASE_DELAY).
    `max_delay`    — cap on per-retry sleep (default from BENCH_BB_RETRY_MAX_DELAY).

    Sleep i = min(delay * 2**i, max_delay). Re-raises the last exception
    if every attempt fails.
    """
    a = attempts if attempts is not None else _DEFAULT_ATTEMPTS
    base = delay if delay is not None else _DEFAULT_BASE_DELAY
    cap = max_delay if max_delay is not None else _DEFAULT_MAX_DELAY
    last: Exception | None = None
    for i in range(a):
        try:
            return fn()
        except _TRANSIENT as exc:
            last = exc
            wait = min(base * (2 ** i), cap)
            log.warning("transient bitbucket error (try %d/%d, wait %.1fs): %s",
                        i + 1, a, wait, exc)
            if i + 1 < a:
                time.sleep(wait)
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
        temp_branch: str = "",
    ):
        self._client = client
        self._project = project
        self._repo = repo
        self._pr_id = _pr_id
        self._agent_username = agent_username
        self._base_url = base_url.rstrip("/")
        # Throw-away branch the bench created server-side so concurrent
        # scenarios sharing the same scenario-source can run side-by-side
        # (Bitbucket allows only one open PR per (from, to) branch pair —
        # without temp branches, parallel runs collide). Empty string ⇒
        # legacy mode where the bench used the scenario branch directly.
        self._temp_branch = temp_branch

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

        # Throw-away branch cleanup. Always delete after declining the PR —
        # leaving these around clutters the test repo and eventually trips
        # the next session's pre-decline scan.
        if self._temp_branch:
            try:
                await self._run(
                    self._delete_branch_via_rest, self._temp_branch
                )
                log.info("temp branch %s deleted", self._temp_branch)
            except Exception as exc:
                log.warning(
                    "temp branch %s delete failed (non-fatal): %s",
                    self._temp_branch, exc,
                )

    def _delete_branch_via_rest(self, branch_name: str) -> None:
        """DELETE the throw-away branch via Bitbucket's branch-utils plugin.

        Endpoint: DELETE /rest/branch-utils/1.0/projects/{P}/repos/{R}/branches
        Body: {"name": "refs/heads/<name>", "dryRun": false}
        """
        path = (
            f"rest/branch-utils/1.0/projects/{self._project}"
            f"/repos/{self._repo}/branches"
        )
        # atlassian-python-api's `delete()` doesn't pass DELETE bodies as
        # JSON cleanly (no `json=` kwarg, and `data=` flow loses the
        # Content-Type), so go through the underlying requests session
        # directly. SSL/cert config still applies because we share the
        # session.
        url = f"{self._client.url.rstrip('/')}/{path}"
        resp = self._client._session.delete(
            url,
            json={
                "name": f"refs/heads/{branch_name}",
                "dryRun": False,
            },
            headers={"X-Atlassian-Token": "no-check"},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise ProviderError(
                f"delete branch {branch_name} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )

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

    # Pre-session cleanup runs at most once per CLI process. Multiple
    # build_proxy calls (gentle mode = sequential, aggressive mode =
    # parallel) must NOT each decline every open [BENCHMARK] PR — in
    # aggressive mode that would tear down a sibling slot's PR mid-run
    # ("only one PR open" anomaly). Set on the first build(); cleared
    # when the process exits.
    _cleanup_done: bool = False
    _cleanup_lock: asyncio.Lock | None = None

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

        loop = asyncio.get_running_loop()

        # Generate a unique throw-away branch name. Lets concurrent
        # scenarios that share the same scenario source branch coexist —
        # each scenario opens a PR from its own bench/* branch instead
        # of fighting over the one (from, to) PR slot.
        import uuid as _uuid
        scenario_id = pr_cfg.get("title", "")
        scen_tag = ""
        for tag in ("SCEN-",):
            i = scenario_id.find(tag)
            if i >= 0:
                end = scenario_id.find(":", i)
                scen_tag = scenario_id[i:end if end > 0 else i + 16].strip()
                break
        scen_tag = scen_tag.replace(" ", "-")[:24] or "BENCH"
        temp_branch = f"bench/{scen_tag}/{_uuid.uuid4().hex[:8]}"

        # Pre-clean orphan bench/ branches (and any stale [BENCHMARK] PRs
        # on the same target) left behind by killed runs. Best-effort —
        # logged failures don't block the new run. Runs at most once
        # per CLI process; subsequent build() calls (gentle mode loop
        # or aggressive mode fan-out) skip it because in-flight
        # [BENCHMARK] PRs from sibling slots are NOT stale and must
        # not be declined.
        if cls._cleanup_lock is None:
            cls._cleanup_lock = asyncio.Lock()
        async with cls._cleanup_lock:
            if not cls._cleanup_done:
                await loop.run_in_executor(
                    None,
                    lambda: cls._cleanup_stale_bench_artefacts(
                        client, project, repo, pr_cfg["to_branch"],
                    ),
                )
                cls._cleanup_done = True

        # Create the throw-away branch server-side from the scenario
        # source. No git push needed — Bitbucket's branch-utils plugin
        # creates it from a server-known startPoint. Wrapped in _retry
        # so a single SSL EOF / connection-reset doesn't kill the
        # scenario when the server is under load.
        await loop.run_in_executor(
            None,
            lambda: _retry(lambda: cls._create_branch_via_rest(
                client, project, repo, temp_branch, pr_cfg["from_branch"],
            )),
        )

        payload = {
            "title": pr_cfg.get("title", "[BENCHMARK]"),
            "description": pr_cfg.get("description", "Auto-created by benchmark"),
            "state": "OPEN",
            "fromRef": {"id": f"refs/heads/{temp_branch}"},
            "toRef": {"id": f"refs/heads/{pr_cfg['to_branch']}"},
            "reviewers": [],
        }

        try:
            resp = await loop.run_in_executor(
                None,
                lambda: _retry(lambda: client.create_pull_request(project, repo, payload)),
            )
        except Exception as exc:
            # If PR creation failed, the temp branch is now orphaned.
            # Try to delete it immediately so we don't leave litter.
            try:
                await loop.run_in_executor(
                    None,
                    lambda: cls._delete_branch_via_rest_static(
                        client, project, repo, temp_branch,
                    ),
                )
            except Exception:
                pass
            raise ProviderError(f"Failed to create PR: {exc}") from exc

        if not resp or "id" not in resp:
            raise ProviderError(f"Unexpected response when creating PR: {resp!r}")

        return RealBitbucketPRProxy(
            client, project, repo, resp["id"], agent_username, base_url,
            temp_branch=temp_branch,
        )

    @staticmethod
    def _create_branch_via_rest(client: Bitbucket, project: str, repo: str,
                                name: str, start_point: str) -> None:
        path = (
            f"rest/branch-utils/1.0/projects/{project}"
            f"/repos/{repo}/branches"
        )
        resp = client.post(
            path,
            json={
                "name": name,
                "startPoint": f"refs/heads/{start_point}",
            },
            headers={"X-Atlassian-Token": "no-check"},
            advanced_mode=True,
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"create branch {name} from {start_point} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )

    @staticmethod
    def _delete_branch_via_rest_static(client: Bitbucket, project: str,
                                       repo: str, name: str) -> None:
        """Same call as RealBitbucketPRProxy._delete_branch_via_rest but
        usable from the factory before a proxy exists."""
        path = (
            f"rest/branch-utils/1.0/projects/{project}"
            f"/repos/{repo}/branches"
        )
        url = f"{client.url.rstrip('/')}/{path}"
        resp = client._session.delete(
            url,
            json={"name": f"refs/heads/{name}", "dryRun": False},
            headers={"X-Atlassian-Token": "no-check"},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            log.warning("delete branch %s: HTTP %d %s",
                        name, resp.status_code, resp.text[:200])

    @classmethod
    def _cleanup_stale_bench_artefacts(cls, client: Bitbucket,
                                       project: str, repo: str,
                                       to_branch: str) -> None:
        """Clean up litter from interrupted prior runs:
        1. Decline any open `[BENCHMARK]` PRs targeting *to_branch*.
        2. Delete every `bench/*` branch that no longer has an open PR.

        Both passes are best-effort — failures are logged, not raised.
        Catches scenarios killed by Ctrl+C / SIGKILL / network drop where
        __aexit__ never ran.
        """
        cls._decline_open_bench_prs(client, project, repo, to_branch)
        cls._delete_orphan_bench_branches(client, project, repo)

    @staticmethod
    def _decline_open_bench_prs(client: Bitbucket, project: str, repo: str,
                                to_branch: str) -> None:
        """Walk all open PRs targeting *to_branch* and decline the
        ``[BENCHMARK]`` ones. Doesn't touch real users' PRs."""
        path = f"rest/api/1.0/projects/{project}/repos/{repo}/pull-requests"
        target_ref = f"refs/heads/{to_branch}"
        start = 0
        while True:
            try:
                resp = _retry(lambda: client.get(
                    path,
                    params={
                        "state": "OPEN",
                        "at": target_ref,
                        "direction": "INCOMING",
                        "limit": 100,
                        "start": start,
                    },
                    advanced_mode=True,
                ))
                if resp.status_code >= 400:
                    log.warning("list open PRs failed: HTTP %d %s",
                                resp.status_code, resp.text[:200])
                    return
                data = resp.json() or {}
            except Exception as exc:
                log.warning("list open PRs failed: %s", exc)
                return

            for pr in (data.get("values") or []):
                if (pr.get("toRef") or {}).get("id") != target_ref:
                    continue
                title = pr.get("title", "") or ""
                if "[BENCHMARK]" not in title:
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
                        log.info("declined stale BENCHMARK PR #%s -> %s",
                                 pr_id, to_branch)
                except Exception as exc:
                    log.warning("decline stale PR #%s raised: %s", pr_id, exc)

            if data.get("isLastPage", True):
                return
            start = data.get("nextPageStart", 0) or (start + len(data.get("values") or []))

    @classmethod
    def _delete_orphan_bench_branches(cls, client: Bitbucket,
                                      project: str, repo: str) -> None:
        """Delete every `bench/*` branch that has no open PR pointing at
        it. The throw-away branches our build() creates only matter while
        their PR is open; once declined they're litter."""
        path = f"rest/api/1.0/projects/{project}/repos/{repo}/branches"
        start = 0
        bench_branches: list[str] = []
        while True:
            try:
                resp = _retry(lambda: client.get(
                    path,
                    params={
                        "filterText": "bench/",
                        "limit": 100,
                        "start": start,
                    },
                    advanced_mode=True,
                ))
                if resp.status_code >= 400:
                    log.warning("list bench branches failed: HTTP %d %s",
                                resp.status_code, resp.text[:200])
                    return
                data = resp.json() or {}
            except Exception as exc:
                log.warning("list bench branches failed: %s", exc)
                return
            for b in (data.get("values") or []):
                name = (b.get("displayId") or "").strip()
                if name.startswith("bench/"):
                    bench_branches.append(name)
            if data.get("isLastPage", True):
                break
            start = data.get("nextPageStart", 0) or (start + len(data.get("values") or []))

        if not bench_branches:
            return

        # Find which of these branches still have an open PR.
        open_pr_path = f"rest/api/1.0/projects/{project}/repos/{repo}/pull-requests"
        live: set[str] = set()
        for branch in bench_branches:
            try:
                resp = client.get(
                    open_pr_path,
                    params={
                        "state": "OPEN",
                        "at": f"refs/heads/{branch}",
                        "direction": "OUTGOING",
                        "limit": 1,
                    },
                    advanced_mode=True,
                )
                if resp.status_code < 400 and (resp.json() or {}).get("size", 0) > 0:
                    live.add(branch)
            except Exception:
                # If we can't confirm, leave the branch alone — better
                # litter than wiping a live PR's source.
                live.add(branch)

        for branch in bench_branches:
            if branch in live:
                continue
            try:
                cls._delete_branch_via_rest_static(client, project, repo, branch)
                log.info("deleted orphan bench branch %s", branch)
            except Exception as exc:
                log.warning("delete orphan bench branch %s raised: %s",
                            branch, exc)
