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
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import json
import logging

log = logging.getLogger(__name__)


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
    import ssl
    from urllib.error import HTTPError
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    ctx = ssl.create_default_context(cafile=ca_bundle)
    if client_cert:
        ctx.load_cert_chain(client_cert)
    try:
        with urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise HTTPError(e.url, e.code, _read_http_error(e), e.headers, None) from None


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


def _clone_url(server_url: str, project: str, repo: str, token: str = "") -> str:
    """Build the HTTP clone URL with optional token auth embedded."""
    parsed = urlparse(server_url)
    if token:
        # x-token-auth:{token}@host — works with Bitbucket Server, avoids http.extraHeader
        return f"{parsed.scheme}://x-token-auth:{token}@{parsed.netloc}{parsed.path}/scm/{project.lower()}/{repo}.git"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}/scm/{project.lower()}/{repo}.git"


# ── git helpers ───────────────────────────────────────────────────────────────

def _git_base_env() -> dict:
    """Base env: disable interactive prompts."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"   # never ask for credentials
    env["GIT_ASKPASS"] = "echo"        # return empty string for any git password prompt
    return env


def _ssl_flags(ca_bundle: str | None, client_cert: str | None) -> list[str]:
    """Return git -c flags for SSL CA and client cert."""
    flags: list[str] = []
    if ca_bundle:
        flags += ["-c", f"http.sslCAInfo={ca_bundle}"]
    if client_cert:
        flags += ["-c", f"http.sslCert={client_cert}"]
    return flags


def _run(args: list[str], cwd: str | None = None) -> str:
    env = _git_base_env()
    result = subprocess.run(
        args, env=env, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed: {' '.join(args)}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout.decode(errors="replace")


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

    Returns:
        (diff_text, repo_path, cleanup_fn)

    cleanup_fn() removes the temp directory — call it when done.
    """
    token      = token      or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle  = ca_bundle  or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    emit = on_status or (lambda msg: None)

    # 1. Parse URL
    server_url, project, repo, pr_id = parse_pr_url(pr_url)
    emit(f"Fetching PR metadata: {server_url} / {project} / {repo} #{pr_id}")

    # 2. Get PR metadata from API
    pr = _get_pr_meta(server_url, project, repo, pr_id, token, ca_bundle, client_cert)
    from_branch  = pr["fromRef"]["displayId"]
    from_sha     = pr["fromRef"]["latestCommit"]
    to_sha       = pr["toRef"]["latestCommit"]
    pr_meta      = {
        "title":       pr.get("title", ""),
        "description": pr.get("description", "") or "",
        "author":      pr.get("author", {}).get("user", {}).get("displayName", ""),
        "from_branch": from_branch,
        "to_branch":   pr["toRef"]["displayId"],
        "pr_id":       pr_id,
        "base_ref":    to_sha,
        "source_ref":  from_sha,
    }
    log.debug("fromRef=%s (%s)  toRef sha=%s", from_branch, from_sha, to_sha)

    clone_url = _clone_url(server_url, project, repo, token=token)
    # Log URL without token for safety
    safe_url = _clone_url(server_url, project, repo)
    emit(f"Cloning {safe_url}  branch={from_branch}")

    # 3. Clone: blobless + single branch → fast, full working tree
    tmpdir = tempfile.mkdtemp(prefix="diffgraph-")

    ssl_flags = _ssl_flags(ca_bundle, client_cert)
    git_cfg = ssl_flags  # auth via URL token, not http.extraHeader

    try:
        _run([
            "git", *git_cfg,
            "clone",
            "--filter=blob:none",
            "--single-branch", "--branch", from_branch,
            clone_url, tmpdir,
        ])

        # Bake SSL into the repo config so all subsequent git ops
        # (fetch, diff lazy-blob downloads) work. Auth is in the origin URL.
        if ca_bundle:
            _run(["git", "config", "http.sslCAInfo", ca_bundle], cwd=tmpdir)
        if client_cert:
            _run(["git", "config", "http.sslCert", client_cert], cwd=tmpdir)

        # 4. Fetch base branch history (no depth limit — only commits+trees,
        #    no blobs, so still fast). Full history is needed to find the
        #    merge-base for a correct three-dot diff matching the PR UI.
        emit(f"Fetching base commit {to_sha[:12]}…")
        _run([
            "git", "fetch", "--filter=blob:none",
            "origin", to_sha,
        ], cwd=tmpdir)

        # 5. Three-dot diff: changes from merge-base to fromRef — matches PR UI.
        emit("Computing diff…")
        diff_text = _run(
            ["git", "diff", f"{to_sha}...{from_sha}"],
            cwd=tmpdir,
        )

    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    def cleanup() -> None:
        shutil.rmtree(tmpdir, ignore_errors=True)

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
    comment_meta: dict | None = None,
) -> int:
    """
    Post review comments to a Bitbucket Server PR as inline anchored comments.
    Returns the number of successfully posted comments.

    changed_lines: optional dict mapping file path → set of changed line numbers
    (1-indexed, from diff_result.files[path].after_changed_lines).
    When provided, each comment's line is snapped to the nearest changed line so
    the Bitbucket anchor is valid. Comments whose file has no changed lines are
    posted without an anchor (general PR comment).
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
        body = _build_comment_body(c, meta=comment_meta)
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
            comment_obj = activity.get("comment", {})
            anchor = activity.get("commentAnchor") or {}
            comments.append({
                "id":       comment_obj.get("id"),
                "file":     anchor.get("path", ""),
                "line":     anchor.get("line", 0),
                "text":     comment_obj.get("text", ""),
                "author":   comment_obj.get("author", {}).get("displayName", ""),
                "resolved": comment_obj.get("state", "") == "RESOLVED",
                "anchored": bool(anchor.get("path")),
            })
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
    comment_meta: dict | None = None,
) -> None:
    """Post a reply to an existing PR comment thread."""
    token       = token       or os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN") or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN")
    ca_bundle   = ca_bundle   or os.environ.get("REQUESTS_CA_BUNDLE")
    client_cert = client_cert or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")

    if not token:
        raise ValueError("BITBUCKET_SERVER_BEARER_TOKEN is required")

    if comment_meta:
        gen = comment_meta.get("gen", "")
        h = comment_meta.get("hash", "")
        run = comment_meta.get("run", "")
        text += f"\n\n`dg:{gen}:{h}:{run}`"

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


def _api_put(url: str, token: str, ca_bundle: str | None, client_cert: str | None, payload: dict) -> dict:
    import ssl
    from urllib.error import HTTPError
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


def _build_comment_body(c, meta: dict | None = None) -> str:
    text = c.comment
    if c.suggestion:
        text += f"\n\n**Suggestion:** {c.suggestion}"
    if meta:
        gen = meta.get("gen", "")
        h = meta.get("hash", "")
        run = meta.get("run", "")
        text += f"\n\n`dg:{gen}:{h}:{run}`"
    return text


def _api_post(url: str, token: str, ca_bundle: str | None, client_cert: str | None, payload: dict) -> dict:
    import ssl
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
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
