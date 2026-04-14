"""
Git repo provider — clone, fetch, diff with configurable auth.

Auth methods (git.auth config or DIFFGRAPH_GIT_AUTH env):
- "header"   — http.extraHeader with Bearer token (Linux default)
- "askpass"  — GIT_ASKPASS with token (Windows with Bearer token)
- "userpass" — GIT_ASKPASS with username/password
- "interactive" — prompt for credentials, offer to save
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .base import RepoProvider

log = logging.getLogger(__name__)


@dataclass
class GitAuthConfig:
    method: str = ""        # header | askpass | userpass | interactive | auto
    token: str = ""         # Bearer token (for header/askpass)
    username: str = ""      # for userpass
    password: str = ""      # for userpass
    ssh_port: int = 7999    # Bitbucket Server SSH port
    ca_bundle: str = ""
    client_cert: str = ""


class GitRepoProvider(RepoProvider):

    def __init__(self, auth: GitAuthConfig | None = None):
        self.auth = auth or _auto_auth_config()
        self._askpass_file: str | None = None

    def clone(self, url: str, branch: str, dest: str) -> None:
        ssl_flags = self._ssl_flags()
        methods = self._auth_methods()
        last_err = None

        for method_name, git_cfg, env in methods:
            try:
                # SSH uses different URL, no SSL flags
                if method_name == "ssh":
                    clone_url = self._https_to_ssh(url)
                    clone_ssl = []
                else:
                    clone_url = url
                    clone_ssl = ssl_flags
                cmd = ["git", *git_cfg, *clone_ssl, "clone", "--filter=blob:none",
                       "--single-branch", "--branch", branch, clone_url, dest]
                log.debug("git clone [%s]: %s", method_name, _safe_cmd(cmd))
                self._run(cmd, env=env)
                return  # success
            except RuntimeError as exc:
                last_err = exc
                log.warning("git clone failed with %s: %s", method_name, str(exc)[:200])
                import shutil
                shutil.rmtree(dest, ignore_errors=True)
                import os as _os
                _os.makedirs(dest, exist_ok=True)

        # All methods failed → try interactive as last resort
        if self.auth.method in ("auto", ""):
            log.info("all auth methods failed, trying interactive credentials")
            try:
                username, password = _interactive_credentials()
                if username:
                    env = _git_base_env()
                    askpass = _make_askpass_userpass(username, password)
                    env["GIT_ASKPASS"] = askpass
                    cmd = ["git", "-c", "credential.helper=", *ssl_flags,
                           "clone", "--filter=blob:none",
                           "--single-branch", "--branch", branch, url, dest]
                    self._run(cmd, env=env)
                    return
            except RuntimeError as exc:
                last_err = exc

        raise last_err or RuntimeError("git clone failed: no auth methods available")

    def fetch(self, repo_path: str, ref: str) -> None:
        cmd = ["git", "fetch", "--filter=blob:none", "origin", ref]
        log.debug("git fetch: %s", ref)
        self._run(cmd, cwd=repo_path)

    def diff(self, repo_path: str, base: str, source: str) -> str:
        cmd = ["git", "diff", f"{base}...{source}"]
        return self._run(cmd, cwd=repo_path)

    def log_oneline(self, repo_path: str, base: str, source: str) -> str:
        cmd = ["git", "log", "--oneline", "--reverse", f"{base}..{source}"]
        try:
            return self._run(cmd, cwd=repo_path).strip()
        except Exception:
            return "(unavailable)"

    def configure_repo(self, repo_path: str) -> None:
        """Bake auth + SSL into repo config for subsequent git ops."""
        log.debug("configuring repo auth in %s", repo_path)
        # Disable credential manager
        self._run(["git", "config", "credential.helper", ""], cwd=repo_path)
        # Auth
        if self.auth.token:
            self._run(["git", "config", "http.extraHeader",
                        f"Authorization: Bearer {self.auth.token}"], cwd=repo_path)
        # SSL
        if self.auth.ca_bundle:
            self._run(["git", "config", "http.sslCAInfo", self.auth.ca_bundle], cwd=repo_path)
        if self.auth.client_cert:
            self._run(["git", "config", "http.sslCert", self.auth.client_cert], cwd=repo_path)

    def cleanup(self) -> None:
        """Remove temp askpass script."""
        if self._askpass_file:
            try:
                os.unlink(self._askpass_file)
            except OSError:
                pass
            self._askpass_file = None

    # ── Internal ──────────────────────────────────────────────

    def _auth_methods(self) -> list[tuple[str, list[str], dict]]:
        """Return list of (name, git_cfg, env) to try in order.

        For explicit method: returns just that one.
        For auto: returns all available methods as fallback chain.
        """
        method = self.auth.method
        candidates: list[tuple[str, list[str], dict]] = []

        # All methods include credential.helper= to disable Windows credential manager
        disable_cred = ["-c", "credential.helper="]

        if method in ("header", "auto", "") and self.auth.token:
            env = _git_base_env()
            candidates.append((
                "header",
                disable_cred + ["-c", f"http.extraHeader=Authorization: Bearer {self.auth.token}"],
                env,
            ))

        if method in ("askpass", "auto", "") and self.auth.token:
            env = _git_base_env()
            askpass = _make_askpass_token(self.auth.token)
            env["GIT_ASKPASS"] = askpass
            candidates.append(("askpass-token", disable_cred, env))

        if method in ("ssh", "auto", ""):
            # SSH via ssh-agent — no token/password needed, different transport
            env = _git_base_env()
            candidates.append(("ssh", [], env))

        if method in ("userpass", "auto", "") and self.auth.username and self.auth.password:
            env = _git_base_env()
            askpass = _make_askpass_userpass(self.auth.username, self.auth.password)
            env["GIT_ASKPASS"] = askpass
            candidates.append(("userpass", disable_cred, env))

        if method == "interactive" or (not candidates and method in ("auto", "")):
            username, password = _interactive_credentials()
            if username:
                env = _git_base_env()
                askpass = _make_askpass_userpass(username, password)
                env["GIT_ASKPASS"] = askpass
                candidates.append(("interactive", [], env))

        if not candidates:
            log.warning("git auth: no credentials available")
            candidates.append(("none", [], _git_base_env()))

        # For explicit method (not auto): only first match
        if method and method not in ("auto", ""):
            log.info("git auth: %s", candidates[0][0])
            return candidates[:1]

        log.info("git auth chain: %s", " → ".join(c[0] for c in candidates))
        return candidates

    def _https_to_ssh(self, https_url: str) -> str:
        """Convert HTTPS clone URL to SSH URL.

        https://server/scm/project/repo.git → ssh://git@server:7999/project/repo.git
        """
        from urllib.parse import urlparse
        parsed = urlparse(https_url)
        # Bitbucket Server HTTPS: /scm/project/repo.git
        # Bitbucket Server SSH:   /project/repo.git
        path = parsed.path
        if "/scm/" in path:
            path = path.split("/scm/", 1)[1]
            path = "/" + path
        ssh_url = f"ssh://git@{parsed.hostname}:{self.auth.ssh_port}{path}"
        log.debug("SSH URL: %s → %s", https_url, ssh_url)
        return ssh_url

    def _ssl_flags(self) -> list[str]:
        flags = []
        if self.auth.ca_bundle:
            flags += ["-c", f"http.sslCAInfo={self.auth.ca_bundle}"]
        if self.auth.client_cert:
            flags += ["-c", f"http.sslCert={self.auth.client_cert}"]
        return flags

    def _run(self, args: list[str], cwd: str | None = None, env: dict | None = None) -> str:
        run_env = env or _git_base_env()
        result = subprocess.run(
            args, env=run_env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            log.error("git failed (exit %d): %s", result.returncode, stderr[:300])
            raise RuntimeError(f"git command failed: {_safe_cmd(args)}\nstderr: {stderr}")
        return result.stdout.decode(errors="replace")


# ── Helpers ───────────────────────────────────────────────────

def _git_base_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    return env


def _auto_auth_config() -> GitAuthConfig:
    """Build auth config from environment variables."""
    return GitAuthConfig(
        method=os.environ.get("DIFFGRAPH_GIT_AUTH", "auto"),
        token=(os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN", "")
               or os.environ.get("BITBUCKET_SERVER__BEARER_TOKEN", "")),
        username=os.environ.get("BITBUCKET_USERNAME", ""),
        password=os.environ.get("BITBUCKET_PASSWORD", ""),
        ssh_port=int(os.environ.get("BITBUCKET_SSH_PORT", "7999")),
        ca_bundle=os.environ.get("REQUESTS_CA_BUNDLE", ""),
        client_cert=(os.environ.get("BITBUCKET_SERVER_CLIENT_CERT", "")
                     or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT", "")),
    )


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _make_askpass_token(token: str) -> str:
    return _write_askpass(
        _TEMPLATES_DIR / "git-askpass.sh",
        {"{{ token }}": token},
    )


def _make_askpass_userpass(username: str, password: str) -> str:
    return _write_askpass(
        _TEMPLATES_DIR / "git-askpass-userpass.sh",
        {"{{ username }}": username, "{{ password }}": password},
    )


def _write_askpass(template_path: Path, replacements: dict) -> str:
    text = template_path.read_text()
    for k, v in replacements.items():
        text = text.replace(k, v)
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="git-askpass-",
    )
    f.write(text)
    f.close()
    os.chmod(f.name, stat.S_IRWXU)
    log.debug("askpass script: %s", f.name)
    return f.name


def _interactive_credentials() -> tuple[str, str]:
    import getpass
    print("\nNo git credentials found.")
    username = input("  Username: ").strip()
    password = getpass.getpass("  Password: ").strip()
    if username and password:
        save = input("  Save to .env? [y/N]: ").strip().lower()
        if save == "y":
            _save_to_env(username, password)
    return username, password


def _save_to_env(username: str, password: str) -> None:
    env_path = Path(".env")
    lines = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    lines = [l for l in lines
             if not l.lstrip("export ").startswith("BITBUCKET_USERNAME=")
             and not l.lstrip("export ").startswith("BITBUCKET_PASSWORD=")]
    lines.append(f"export BITBUCKET_USERNAME={username}")
    lines.append(f"export BITBUCKET_PASSWORD={password}")
    env_path.write_text("\n".join(lines) + "\n")
    print(f"  Saved to {env_path.resolve()}")


def _safe_cmd(args: list[str]) -> str:
    """Redact tokens from command for logging."""
    return " ".join(a if "Bearer" not in a else "***" for a in args)
