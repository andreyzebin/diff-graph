"""
Bitbucket Server provider for DiffGraph.

Fetches a PR diff and clones the source branch into a temp directory so
DiffGraph can run its full analysis without any pre-existing local checkout.

Environment variables (same names as pr-agent):
    BITBUCKET_SERVER_BEARER_TOKEN   Bearer token for auth
    BITBUCKET_SERVER_CLIENT_CERT    Path to client PEM (mTLS, optional)
    REQUESTS_CA_BUNDLE              Path to CA bundle for SSL verification
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import json
import logging

log = logging.getLogger(__name__)


# ── Centralised retry on transient Bitbucket errors ───────────────────────────
#
# Corp Bitbucket Server under load occasionally drops a request mid-handshake
# (`SSL: UNEXPECTED_EOF_WHILE_READING`, `Connection reset`, `socket.timeout`).
# Every call into the API goes through `_with_retry()` so a single blip
# doesn't kill a review or comment-post. HTTPError (real 4xx/5xx with a body
# that came back from the server) is NOT retried — that's a logic error, not
# a transport blip.
#
# Tunables, picked up from env so the agent and the bench can be tightened
# without code changes:
#   DIFFGRAPH_BB_RETRY_ATTEMPTS=N      total tries (default 5)
#   DIFFGRAPH_BB_RETRY_BASE_DELAY=SEC  first-retry sleep (default 1.0)
#   DIFFGRAPH_BB_RETRY_MAX_DELAY=SEC   cap per-retry sleep (default 30)
# Backoff is exponential: delay_i = min(base * 2**i, max).

_TRANSIENT_TYPES: tuple = (
    ssl.SSLError,
    socket.timeout,
    ConnectionError,           # covers ConnectionResetError / ConnectionRefusedError
    TimeoutError,
)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        # Real HTTP response (4xx/5xx) — don't retry by default.
        return False
    if isinstance(exc, URLError):
        # URLError wraps the underlying transport error in `.reason`.
        return isinstance(exc.reason, _TRANSIENT_TYPES)
    return isinstance(exc, _TRANSIENT_TYPES)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _with_retry(fn):
    """Run fn() with exponential-backoff retry on transient transport errors."""
    attempts = _env_int("DIFFGRAPH_BB_RETRY_ATTEMPTS", 5)
    base = _env_float("DIFFGRAPH_BB_RETRY_BASE_DELAY", 1.0)
    cap = _env_float("DIFFGRAPH_BB_RETRY_MAX_DELAY", 30.0)
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            last = exc
            wait = min(base * (2 ** i), cap)
            log.warning("transient bitbucket error (try %d/%d, wait %.1fs): %s",
                        i + 1, attempts, wait, exc)
            if i + 1 < attempts:
                time.sleep(wait)
    assert last is not None
    raise last


# ── URL parsing ───────────────────────────────────────────────────────────────

def parse_pr_url(pr_url: str) -> tuple[str, str, str, int]:
    """
    Parse a Bitbucket Server PR URL into (server_url, project, repo, pr_id).

    Example:
        https://bitbucket.example.com/projects/MYPROJECT/repos/my-repo/pull-requests/42
        → ("https://bitbucket.example.com", "MYPROJECT", "my-repo", 42)
    """
    parsed = urlparse(pr_url)
    path_parts = parsed.path.strip("/").split("/")

    try:
        proj_idx = path_parts.index("projects")
    except ValueError:
        raise ValueError(f"Cannot find 'projects' segment in PR URL: {pr_url}")

    server_prefix = "/".join(path_parts[:proj_idx])
    base = f"{parsed.scheme}://{parsed.netloc}"
    server_url = f"{base}/{server_prefix}".rstrip("/")

    try:
        project  = path_parts[proj_idx + 1]
        repo     = path_parts[proj_idx + 3]   # projects/X/repos/Y
        pr_id    = int(path_parts[-1])
    except (IndexError, ValueError):
        raise ValueError(f"Malformed Bitbucket Server PR URL: {pr_url}")

    return server_url, project, repo, pr_id


# ── API ───────────────────────────────────────────────────────────────────────

def _api_get(url: str, token: str, ca_bundle: str | None, client_cert: str | None) -> dict:
    def _do() -> dict:
        req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        ctx = ssl.create_default_context(cafile=ca_bundle)
        if client_cert:
            ctx.load_cert_chain(client_cert)
        try:
            with urlopen(req, context=ctx, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            raise HTTPError(e.url, e.code, _read_http_error(e), e.headers, None) from None
    return _with_retry(_do)


def _read_http_error(e) -> str:
    """Extract a readable message from an HTTPError, including the response body."""
    try:
        body = e.read().decode(errors="replace").strip()
        # Bitbucket returns JSON errors like {"errors": [{"message": "..."}]}
        data = json.loads(body)
        msgs = [err.get("message", "") for err in data.get("errors", []) if err.get("message")]
        if msgs:
            return f"HTTP {e.code}: {'; '.join(msgs)}"
    except Exception:
        pass
    return f"HTTP Error {e.code}: {body[:300] if 'body' in dir() else ''}"


def _get_pr_meta(
    server_url: str, project: str, repo: str, pr_id: int,
    token: str, ca_bundle: str | None, client_cert: str | None,
) -> dict:
    url = f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
    return _api_get(url, token, ca_bundle, client_cert)


def get_pr_info(
    pr_url: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> dict:
    """Fetch basic PR metadata (title, description) without cloning."""
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")
    if not token:
        return {}
    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    data = _get_pr_meta(server_url, project, repo, pr_id, token, ca_bundle, client_cert)
    return {
        "title": data.get("title", ""),
        "description": data.get("description", ""),
    }


def _clone_url(server_url: str, project: str, repo: str) -> str:
    """Build the HTTP clone URL: <server>/scm/<project_lower>/<repo>.git"""
    parsed = urlparse(server_url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}/scm/{project.lower()}/{repo}.git"


# ── public API ────────────────────────────────────────────────────────────────

def fetch_pr(
    pr_url: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[str, str, Callable[[], None], dict]:
    """
    Clone the PR source branch and produce a unified diff.

    Uses PRProvider for API calls and RepoProvider for git operations.
    Auth for each is independent.

    Returns:
        (diff_text, repo_path, cleanup_fn, pr_meta)
    """
    from .providers.bitbucket_pr import BitbucketPRProvider
    from .providers.git_repo import GitRepoProvider, GitAuthConfig

    token      = token or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN") or ""
    ca_bundle  = ca_bundle or os.environ.get("REQUESTS_CA_BUNDLE") or ""
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT") or ""

    emit = on_status or (lambda msg: None)

    # ── PR Provider (API) ─────────────────────────────────
    pr_provider = BitbucketPRProvider(token=token, ca_bundle=ca_bundle, client_cert=client_cert)

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    emit(f"Fetching PR metadata: {server_url} / {project} / {repo} #{pr_id}")

    meta = pr_provider.get_pr_meta(pr_url)
    pr_meta = {
        "title":       meta.title,
        "description": meta.description,
        "author":      meta.author,
        "from_branch": meta.from_branch,
        "to_branch":   meta.to_branch,
        "pr_id":       meta.pr_id,
        "base_ref":    meta.to_sha,
        "source_ref":  meta.from_sha,
    }
    log.debug("fromRef=%s (%s)  toRef sha=%s", meta.from_branch, meta.from_sha, meta.to_sha)

    # ── Repo Provider (git) ───────────────────────────────
    git_auth = GitAuthConfig(
        method=os.environ.get("DIFFGRAPH_GIT_AUTH", "header"),
        token=token,
        ssh_port=int(os.environ.get("BITBUCKET_SSH_PORT", "7999")),
        ca_bundle=ca_bundle,
        client_cert=client_cert,
    )
    git = GitRepoProvider(auth=git_auth)

    clone_url = pr_provider.clone_url(pr_url)
    emit(f"Cloning {clone_url}  branch={meta.from_branch}")

    tmpdir = tempfile.mkdtemp(prefix="diffgraph-")

    try:
        git.clone(clone_url, meta.from_branch, tmpdir)
        git.configure_repo(tmpdir)

        emit(f"Fetching base commit {meta.to_sha[:12]}…")
        git.fetch(tmpdir, meta.to_sha)

        emit("Computing diff…")
        diff_text = git.diff(tmpdir, meta.to_sha, meta.from_sha)

    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        git.cleanup()
        raise

    def cleanup() -> None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        git.cleanup()

    emit(f"Ready  repo={tmpdir}  diff={len(diff_text)} chars")
    return diff_text, tmpdir, cleanup, pr_meta


# ── posting review comments ───────────────────────────────────────────────────

def post_review_comments(
    pr_url: str,
    comments: list,  # list[ReviewComment] — avoid circular import
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    on_status: Callable[[str], None] | None = None,
    changed_lines: dict | None = None,
    decorate: Callable[[str], str] | None = None,
) -> int:
    """
    Post review comments to a Bitbucket Server PR as inline anchored comments.
    Returns the number of successfully posted comments.

    changed_lines: optional dict mapping file path → set of changed line numbers
    (1-indexed, from diff_result.files[path].after_changed_lines).
    When provided, each comment's line is snapped to the nearest changed line so
    the Bitbucket anchor is valid. Comments whose file has no changed lines are
    posted without an anchor (general PR comment).

    decorate: optional pure text→text post-processor applied to each body
    before POST (e.g. to append a traceability footer). Transport-agnostic.
    """
    token      = token      or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle  = ca_bundle  or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    emit = on_status or (lambda msg: None)
    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/comments"
    )

    _SEV = {"BLOCKER": "BLOCKER", "MAJOR": "BLOCKER", "MINOR": "NORMAL", "COMMENT": "NORMAL"}

    posted = 0
    for c in comments:
        body = _build_comment_body(c)
        if decorate:
            body = decorate(body)
        anchor = _make_anchor(c.file, c.line, changed_lines)
        payload: dict = {"text": body, "severity": _SEV.get(c.severity, "NORMAL")}
        if anchor:
            payload["anchor"] = anchor
        try:
            _api_post(endpoint, token, ca_bundle, client_cert, payload)
            loc = f"{c.file}:{anchor['line']}" if anchor else c.file
            emit(f"posted [{c.severity}] {loc}")
            posted += 1
        except Exception as exc:
            log.warning("failed to post comment on %s:%s — %s", c.file, c.line, exc)
            emit(f"FAILED {c.file}:{c.line} — {exc}")

    return posted


def _make_anchor(file: str, line: int, changed_lines: dict | None) -> dict | None:
    """
    Build a Bitbucket anchor dict for an inline comment.

    If changed_lines is None, use the raw line (legacy behaviour).
    Otherwise snap to the nearest changed line in the file.
    Returns None when the file has no changed lines — caller should post
    without an anchor.
    """
    if changed_lines is None:
        return {
            "diffType": "EFFECTIVE",
            "path": file,
            "lineType": "ADDED",
            "line": line,
            "fileType": "TO",
        }

    file_changed = changed_lines.get(file)
    if not file_changed:
        return None  # no diff lines for this file — post as general comment

    if line in file_changed:
        snapped = line
    else:
        snapped = min(file_changed, key=lambda l: abs(l - line))

    return {
        "diffType": "EFFECTIVE",
        "path": file,
        "lineType": "ADDED",
        "line": snapped,
        "fileType": "TO",
    }


def get_pr_comments(
    pr_url: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> list[dict]:
    """
    Fetch all activity-level comments for a PR.

    Returns a list of dicts:
      {id, file, line, text, author, resolved, anchored}
    Only inline comments (with a file anchor) are returned.
    General comments (no anchor) are included with file='' and line=0.
    """
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    comments: list[dict] = []
    start = 0

    while True:
        url = (
            f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/pull-requests/{pr_id}/activities?start={start}&limit=100"
        )
        data = _api_get(url, token, ca_bundle, client_cert)
        for activity in data.get("values", []):
            if activity.get("action") != "COMMENTED":
                continue
            root = activity.get("comment", {})
            anchor = activity.get("commentAnchor") or {}
            # Walk the nested replies tree (Bitbucket nests replies inside
            # `comment.comments[]`, not as separate activities) and emit
            # one record per comment with parent_id set. Lets the agent
            # see the message graph instead of a flat list.
            stack: list[tuple[dict, int | None, int]] = [(root, None, 0)]
            while stack:
                node, parent_id, depth = stack.pop()
                author_obj = node.get("author", {})
                comments.append({
                    "id":          node.get("id"),
                    "parent_id":   parent_id,
                    "depth":       depth,
                    "file":        anchor.get("path", ""),
                    "line":        anchor.get("line", 0),
                    "text":        node.get("text", ""),
                    "author":      author_obj.get("displayName", ""),
                    "author_slug": author_obj.get("slug", author_obj.get("name", "")),
                    "resolved":    node.get("state", "") == "RESOLVED",
                    "anchored":    bool(anchor.get("path")),
                })
                # Reverse so that when popped from stack, replies are seen
                # in chronological order. Bitbucket returns replies in
                # creation order under `.comments[]`.
                for child in reversed(node.get("comments") or []):
                    stack.append((child, node.get("id"), depth + 1))
        if data.get("isLastPage", True):
            break
        start = data.get("nextPageStart", start + 100)

    return comments


def reply_to_pr_comment(
    pr_url: str,
    comment_id: int,
    text: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> None:
    """Post a reply to an existing PR comment thread.

    Posts `text` verbatim — any traceability footer must be applied by the
    caller (see diffgraph.comment_meta.CommentMeta.decorate).
    """
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/comments"
    )
    _api_post(endpoint, token, ca_bundle, client_cert, {
        "text": text,
        "parent": {"id": comment_id},
    })


def resolve_pr_comment(
    pr_url: str,
    comment_id: int,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> None:
    """Mark a PR comment thread as resolved."""
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    # Bitbucket Server: PUT the comment with state=RESOLVED
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/comments/{comment_id}"
    )
    # We need the current version first to do an optimistic-locking PUT
    comment = _api_get(endpoint, token, ca_bundle, client_cert)
    version = comment.get("version", 0)
    _api_put(endpoint, token, ca_bundle, client_cert, {
        "version": version,
        "text": comment.get("text", ""),
        "state": "RESOLVED",
    })


def get_comment_thread(
    pr_url: str,
    comment_id: int,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    bot_user: str = "",
    subject_pattern: str = "",
) -> str:
    """
    Fetch the comment thread from root to the given comment.

    Walks the parent chain via Bitbucket API.
    Returns formatted text: one message per line, oldest first.

    When `subject_pattern` (regex with one capture group) and `bot_user`
    are set, comments whose body starts with `[<bot_user>]` are tagged
    `[SELF]` in the rendered header so the agent can tell its own prior
    posts apart from other speakers in the same thread. The captured
    name is also used as the displayed author (overriding Bitbucket's
    displayName) so that simulated multi-author threads — where every
    comment is technically authored by the same Bitbucket account —
    render with distinct attributions.
    """
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        return "(no token — cannot fetch thread)"

    # Bitbucket Server quirk: GET /comments/{id} returns `parent = None`
    # for replies — the parent→child link only exists in the parent's
    # `.comments[]` array. Walking up via `.parent` never finds the root
    # for any reply, so the THREAD rendered to the agent collapses to
    # just the trigger comment. Fix: build the parent_id map by walking
    # the activities feed top-down (already done in get_pr_comments),
    # then walk up via that map.
    try:
        all_comments = get_pr_comments(pr_url, token, ca_bundle, client_cert)
    except Exception:
        return "(failed to fetch PR comments)"

    by_id = {c["id"]: c for c in all_comments if c.get("id") is not None}
    if comment_id not in by_id:
        return f"(comment #{comment_id} not found on PR)"

    chain_ids: list[int] = []
    cur_id: int | None = comment_id
    seen: set[int] = set()
    while cur_id is not None:
        if cur_id in seen:
            break
        seen.add(cur_id)
        chain_ids.append(cur_id)
        cur_id = by_id.get(cur_id, {}).get("parent_id")

    chain_ids.reverse()  # root → ... → trigger
    flat = [
        {
            "id": cid,
            "author": by_id[cid].get("author", "unknown"),
            "author_slug": by_id[cid].get("author_slug", ""),
            "text": by_id[cid].get("text", ""),
        }
        for cid in chain_ids
    ]

    if not flat:
        return "(empty thread)"

    from diffgraph.authors import resolve_author

    blocks = []
    for msg in flat:
        a = resolve_author(msg, bot_user=bot_user, subject_pattern=subject_pattern)
        self_tag = " [SELF]" if a.is_self else ""
        trigger_tag = "  ← YOUR TRIGGER" if msg["id"] == comment_id else ""
        # Borders matter: comment bodies often contain markdown / code /
        # backticks, and a flat join makes the LLM blur where one ends
        # and the next begins. The dashed ruler is enough boundary —
        # short on tokens, hard to confuse with content.
        blocks.append(
            f"--- #{msg['id']} by [{a.display_name}]{self_tag}{trigger_tag}\n"
            f"{a.body}"
        )
    return "\n".join(blocks) + "\n--- end of thread ---"


def set_review_status(
    pr_url: str,
    user_slug: str,
    status: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> None:
    """Set the agent's reviewer status on the PR (APPROVED / NEEDS_WORK /
    UNAPPROVED). Uses the participants endpoint with the agent's own user
    slug — Bitbucket Server treats the slug embedded in the path as the
    target reviewer, and the bearer token authenticates the caller.

    No-op if user_slug is empty (production path: never set status without
    the bot user being explicitly configured).
    """
    if not user_slug:
        return
    normalised = (status or "").strip().upper()
    if normalised not in ("APPROVED", "NEEDS_WORK", "UNAPPROVED"):
        raise ValueError(
            f"unknown review status {status!r}; expected APPROVED / NEEDS_WORK / UNAPPROVED"
        )

    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")
    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/participants/{user_slug}"
    )
    _api_put(endpoint, token, ca_bundle, client_cert, {"status": normalised})


def post_general_pr_comment(
    pr_url: str,
    text: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> int:
    """Convenience: post a top-level (non-anchored, non-reply) comment.

    Thin wrapper around post_pr_comment for callers that don't need
    anchor / parent semantics.
    """
    return post_pr_comment(pr_url, text=text,
                           token=token, ca_bundle=ca_bundle, client_cert=client_cert)


def react_to_pr_comment(
    pr_url: str,
    comment_id: int,
    emoticon: str,
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> None:
    """Add a reaction emoji to an existing PR comment.

    Bitbucket Server reactions API:
      POST /rest/api/1.0/projects/{P}/repos/{R}/pull-requests/{ID}
           /comments/{commentId}/reactions/{emoticon}

    `emoticon` is the bare name without colons (e.g. "thumbs_up",
    "thumbs_down", "heart", "smile", "tada", "confused", "eyes",
    "rocket"). A leading/trailing ":" is stripped if the caller
    accidentally passes the wrapped form.
    """
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")
    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    name = (emoticon or "").strip().strip(":")
    if not name:
        raise ValueError("emoticon name is required (e.g. 'thumbs_up')")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/comments/{comment_id}/reactions/{name}"
    )
    _api_post(endpoint, token, ca_bundle, client_cert, {})


def post_pr_comment(
    pr_url: str,
    *,
    text: str,
    file: str = "",
    line: int = 0,
    severity: str = "",
    parent_id: int = 0,
    line_type: str = "ADDED",
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
) -> int:
    """Post any kind of PR comment via the unified comments endpoint.

    Bitbucket Server's `POST /pull-requests/{id}/comments` accepts
    a single body shape that varies by which fields are present:

    - general:  `{text}`
    - inline:   `{text, anchor: {path, line, lineType}, severity?}`
    - reply:    `{text, parent: {id}}`

    Pass `file` + `line` for an inline anchored comment. Pass
    `parent_id` for a reply (anchor + parent_id together would reply
    inside an inline thread, but most callers won't need that).

    Returns the new comment's id, or 0 if Bitbucket didn't echo one.
    """
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")
    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    endpoint = (
        f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}"
        f"/pull-requests/{pr_id}/comments"
    )
    body: dict = {"text": text}
    if parent_id:
        body["parent"] = {"id": int(parent_id)}
    if file and line:
        body["anchor"] = {
            "path": file,
            "line": int(line),
            "lineType": (line_type or "ADDED").upper(),
            "fileType": "TO",
        }
        sev = (severity or "").strip().upper()
        if sev in ("BLOCKER", "MAJOR"):
            body["severity"] = "BLOCKER"
        elif sev in ("MINOR", "COMMENT"):
            body["severity"] = "NORMAL"
    resp = _api_post(endpoint, token, ca_bundle, client_cert, body)
    try:
        return int((resp or {}).get("id", 0))
    except (ValueError, TypeError):
        return 0


def _api_put(url: str, token: str, ca_bundle: str | None, client_cert: str | None, payload: dict) -> dict:
    def _do() -> dict:
        data = json.dumps(payload).encode()
        req = Request(
            url, data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="PUT",
        )
        ctx = ssl.create_default_context(cafile=ca_bundle)
        if client_cert:
            ctx.load_cert_chain(client_cert)
        try:
            with urlopen(req, context=ctx, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            raise HTTPError(e.url, e.code, _read_http_error(e), e.headers, None) from None
    return _with_retry(_do)


def _build_comment_body(c) -> str:
    text = c.comment
    if c.suggestion:
        text += f"\n\n**Suggestion:** {c.suggestion}"
    return text


def _api_post(url: str, token: str, ca_bundle: str | None, client_cert: str | None, payload: dict) -> dict:
    def _do() -> dict:
        data = json.dumps(payload).encode()
        req = Request(
            url, data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        ctx = ssl.create_default_context(cafile=ca_bundle)
        if client_cert:
            ctx.load_cert_chain(client_cert)
        try:
            with urlopen(req, context=ctx, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            raise HTTPError(e.url, e.code, _read_http_error(e), e.headers, None) from None
    return _with_retry(_do)
