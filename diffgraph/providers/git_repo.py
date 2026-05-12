"""
Git repo provider — clone, fetch, diff.

Auth via DIFFGRAPH_GIT_AUTH env var:
- "header" (default) — http.extraHeader with Bearer token
- "ssh" — SSH URL via ssh-agent
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

from .base import RepoProvider

log = logging.getLogger(__name__)


@dataclass
class GitAuthConfig:
    method: str = "header"  # header | ssh
    token: str = ""
    ssh_port: int = 7999
    ca_bundle: str = ""
    client_cert: str = ""


class GitRepoProvider(RepoProvider):

    def __init__(self, auth: GitAuthConfig | None = None):
        self.auth = auth or _auto_config()

    def clone(self, url: str, branch: str, dest: str) -> None:
        if self.auth.method == "ssh":
            clone_url = self._https_to_ssh(url)
            git_cfg: list[str] = []
            log.info("git clone [ssh]: %s branch=%s", clone_url, branch)
        else:
            clone_url = url
            git_cfg = [
                "-c", "credential.helper=",
                "-c", f"http.extraHeader=Authorization: Bearer {self.auth.token}",
                *self._ssl_flags(),
            ]
            log.info("git clone [header]: %s branch=%s", url, branch)

        cmd = ["git", *git_cfg, "clone", "--filter=blob:none",
               "--single-branch", "--branch", branch, clone_url, dest]
        self._run(cmd)

    def fetch(self, repo_path: str, ref: str) -> None:
        # Bitbucket Server typically rejects `--filter=blob:none`
        # (emits `warning: filtering not recognized by server`) and
        # returns an incomplete object set — subsequent
        # `git diff --name-status BASE SOURCE` then silently produces
        # empty output, so the VFS materialisation reports zero
        # changed files. Try the blobless fetch first for speed on
        # servers that DO support it; if the trees we just fetched
        # don't actually contain anything useful, retry without the
        # filter. The cheap probe: `git ls-tree -- <ref>` lists the
        # top-level entries of that commit's tree. An empty result
        # means the trees weren't fetched, even though `cat-file -t`
        # might happily report `commit`.
        self._run(["git", "fetch", "--filter=blob:none", "origin", ref], cwd=repo_path)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        probe = subprocess.run(
            ["git", "ls-tree", "--name-only", ref],
            env=env, cwd=repo_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            log.info(
                "git fetch: blobless %s landed with empty/missing tree "
                "(rc=%d) — retrying full fetch", ref[:12], probe.returncode,
            )
            self._run(["git", "fetch", "origin", ref], cwd=repo_path)

    def diff(self, repo_path: str, base: str, source: str) -> str:
        return self._run(["git", "diff", f"{base}...{source}"], cwd=repo_path)

    def log_oneline(self, repo_path: str, base: str, source: str) -> str:
        try:
            return self._run(
                ["git", "log", "--oneline", "--reverse", f"{base}..{source}"],
                cwd=repo_path,
            ).strip()
        except Exception:
            return "(unavailable)"

    def configure_repo(self, repo_path: str) -> None:
        """Bake auth + SSL into repo config for subsequent git ops."""
        self._run(["git", "config", "credential.helper", ""], cwd=repo_path)
        if self.auth.token:
            self._run(["git", "config", "http.extraHeader",
                        f"Authorization: Bearer {self.auth.token}"], cwd=repo_path)
        if self.auth.ca_bundle:
            self._run(["git", "config", "http.sslCAInfo", self.auth.ca_bundle], cwd=repo_path)
        if self.auth.client_cert:
            self._run(["git", "config", "http.sslCert", self.auth.client_cert], cwd=repo_path)

    def cleanup(self) -> None:
        """No-op — simplified auth has no temp files to clean."""
        pass

    # ── Internal ──────────────────────────────────────────────

    def _https_to_ssh(self, https_url: str) -> str:
        """https://server/scm/project/repo.git → ssh://git@server:port/project/repo.git"""
        parsed = urlparse(https_url)
        path = parsed.path
        if "/scm/" in path:
            path = "/" + path.split("/scm/", 1)[1]
        ssh_url = f"ssh://git@{parsed.hostname}:{self.auth.ssh_port}{path}"
        log.debug("SSH URL: %s", ssh_url)
        return ssh_url

    def _ssl_flags(self) -> list[str]:
        flags: list[str] = []
        if self.auth.ca_bundle:
            flags += ["-c", f"http.sslCAInfo={self.auth.ca_bundle}"]
        if self.auth.client_cert:
            flags += ["-c", f"http.sslCert={self.auth.client_cert}"]
        return flags

    def _run(self, args: list[str], cwd: str | None = None) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            args, env=env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            log.error("git failed (exit %d): %s", result.returncode, stderr[:300])
            raise RuntimeError(f"git failed: {stderr}")
        return result.stdout.decode(errors="replace")


def _auto_config() -> GitAuthConfig:
    return GitAuthConfig(
        method=os.environ.get("DIFFGRAPH_GIT_AUTH", "header"),
        token=(os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN", "")
               or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN", "")),
        ssh_port=int(os.environ.get("BITBUCKET_SSH_PORT", "7999")),
        ca_bundle=os.environ.get("REQUESTS_CA_BUNDLE", ""),
        client_cert=(os.environ.get("BITBUCKET_SERVER_CLIENT_CERT", "")
                     or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT", "")),
    )
