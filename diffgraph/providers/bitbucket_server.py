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
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    ctx = ssl.create_default_context(cafile=ca_bundle)
    if client_cert:
        ctx.load_cert_chain(client_cert)
    with urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read())


def _get_pr_meta(
    server_url: str, project: str, repo: str, pr_id: int,
    token: str, ca_bundle: str | None, client_cert: str | None,
) -> dict:
    url = f"{server_url}/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
    return _api_get(url, token, ca_bundle, client_cert)


def _clone_url(server_url: str, project: str, repo: str) -> str:
    """Build the HTTP clone URL: <server>/scm/<project_lower>/<repo>.git"""
    parsed = urlparse(server_url)
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
) -> tuple[str, str, Callable[[], None]]:
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
    log.debug("fromRef=%s (%s)  toRef sha=%s", from_branch, from_sha, to_sha)

    clone_url = _clone_url(server_url, project, repo)
    emit(f"Cloning {clone_url}  branch={from_branch}")

    # 3. Clone: blobless + single branch → fast, full working tree
    tmpdir = tempfile.mkdtemp(prefix="diffgraph-")

    auth_flag = ["-c", f"http.extraHeader=Authorization: Bearer {token}"]
    ssl_flags = _ssl_flags(ca_bundle, client_cert)
    git_cfg = auth_flag + ssl_flags   # used only for the initial clone

    try:
        _run([
            "git", *git_cfg,
            "clone",
            "--filter=blob:none",
            "--single-branch", "--branch", from_branch,
            clone_url, tmpdir,
        ])

        # Bake auth + SSL into the repo config so all subsequent git ops
        # (fetch, diff lazy-blob downloads) authenticate automatically.
        _run(["git", "config", "http.extraHeader",
              f"Authorization: Bearer {token}"], cwd=tmpdir)
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
    return diff_text, tmpdir, cleanup


# ── posting review comments ───────────────────────────────────────────────────

def post_review_comments(
    pr_url: str,
    comments: list,  # list[ReviewComment] — avoid circular import
    token: str | None = None,
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> int:
    """
    Post review comments to a Bitbucket Server PR as inline anchored comments.
    Returns the number of successfully posted comments.
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

    posted = 0
    for c in comments:
        body = _build_comment_body(c)
        payload = {
            "text": body,
            "severity": c.severity,
            "anchor": {
                "diffType": "EFFECTIVE",
                "path": c.file,
                "lineType": "ADDED",
                "line": c.line,
                "fileType": "TO",
            },
        }
        try:
            _api_post(endpoint, token, ca_bundle, client_cert, payload)
            emit(f"posted [{c.severity}] {c.file}:{c.line}")
            posted += 1
        except Exception as exc:
            log.warning("failed to post comment on %s:%s — %s", c.file, c.line, exc)
            emit(f"FAILED {c.file}:{c.line} — {exc}")

    return posted


def _build_comment_body(c) -> str:
    text = c.comment
    if c.suggestion:
        text += f"\n\n```suggestion\n{c.suggestion}\n```"
    return text


def _api_post(url: str, token: str, ca_bundle: str | None, client_cert: str | None, payload: dict) -> dict:
    import ssl
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
    with urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read())
