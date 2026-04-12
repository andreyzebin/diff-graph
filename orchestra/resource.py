"""
Resource providers — uniform access to files by URI.

Supported schemes:
  file://   — local filesystem (default)
  bitbucket:// — Bitbucket Server REST API

Relative paths (no scheme) are treated as file:// with CWD resolution.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class ResourceProvider(ABC):
    """Base class for resource providers."""

    @abstractmethod
    def get(self, uri: str) -> str:
        """Fetch resource content as text."""

    @abstractmethod
    def list(self, uri: str) -> list[str]:
        """List child resource URIs."""

    def resolve_hash(self, uri: str) -> str:
        """Resolve URI to a content-addressable hash. Default: empty."""
        return ""


class FileProvider(ResourceProvider):
    """Local filesystem provider. Handles file:// URIs and plain paths."""

    def get(self, uri: str) -> str:
        path = _file_uri_to_path(uri)
        return path.read_text(encoding="utf-8")

    def list(self, uri: str) -> list[str]:
        path = _file_uri_to_path(uri)
        if not path.is_dir():
            return []
        return sorted(str(p) for p in path.iterdir() if p.is_file())


class BitbucketProvider(ResourceProvider):
    """
    Bitbucket Server provider.

    URI format: bitbucket://server/PROJECT/repo/refs/branch/path/to/dir
    """

    def __init__(self):
        self._token = os.environ.get("BITBUCKET_SERVER_BEARER_TOKEN", "")
        self._ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
        self._client_cert = os.environ.get("BITBUCKET_SERVER_CLIENT_CERT")

    def get(self, uri: str) -> str:
        server, project, repo, ref, path = _parse_bitbucket_uri(uri)
        url = (
            f"https://{server}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/raw/{path}?at={ref}"
        )
        return _http_get(url, self._token, self._ca_bundle, self._client_cert)

    def list(self, uri: str) -> list[str]:
        server, project, repo, ref, path = _parse_bitbucket_uri(uri)
        url = (
            f"https://{server}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/files/{path}?at={ref}&limit=100"
        )
        import json
        body = _http_get(url, self._token, self._ca_bundle, self._client_cert)
        data = json.loads(body)
        base = f"bitbucket://{server}/{project}/{repo}/refs/{ref}/{path}"
        base = base.rstrip("/")
        return [f"{base}/{f}" for f in data.get("values", [])]

    def resolve_hash(self, uri: str) -> str:
        """Resolve to the latest commit SHA on this ref."""
        import json
        server, project, repo, ref, _path = _parse_bitbucket_uri(uri)
        url = (
            f"https://{server}/rest/api/1.0/projects/{project}/repos/{repo}"
            f"/commits?until={ref}&limit=1"
        )
        try:
            body = _http_get(url, self._token, self._ca_bundle, self._client_cert)
            data = json.loads(body)
            values = data.get("values", [])
            if values:
                return values[0].get("id", "")
        except Exception:
            pass
        return ""


# ── Registry ─────────────────────────────────────────────────────────────────

_providers: dict[str, ResourceProvider] = {}


def get_provider(uri: str) -> ResourceProvider:
    """Get the appropriate provider for a URI."""
    scheme = _detect_scheme(uri)

    if scheme not in _providers:
        if scheme == "file":
            _providers["file"] = FileProvider()
        elif scheme == "bitbucket":
            _providers["bitbucket"] = BitbucketProvider()
        else:
            raise ValueError(f"unknown resource scheme: {scheme}")

    return _providers[scheme]


def resource_get(uri: str) -> str:
    """Convenience: get resource content."""
    return get_provider(uri).get(uri)


def resource_list(uri: str) -> list[str]:
    """Convenience: list child resources."""
    return get_provider(uri).list(uri)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_scheme(uri: str) -> str:
    """Detect URI scheme. Plain paths → file."""
    if uri.startswith("file://"):
        return "file"
    if uri.startswith("bitbucket://"):
        return "bitbucket"
    # Relative or absolute path → file
    return "file"


def _file_uri_to_path(uri: str) -> Path:
    """Convert file:// URI or plain path to Path."""
    if uri.startswith("file://"):
        # file:///absolute/path or file://relative/path
        path_str = uri[7:]
        if not path_str.startswith("/"):
            path_str = uri[7:]  # relative
        return Path(path_str)
    return Path(uri)


def _parse_bitbucket_uri(uri: str) -> tuple[str, str, str, str, str]:
    """Parse bitbucket://server/PROJECT/repo/refs/branch/path → (server, project, repo, ref, path)."""
    # bitbucket://sberworks.ru/SBLOOM/prompt-library/refs/main/prompts/
    parsed = urlparse(uri)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4 or parts[2] != "refs":
        raise ValueError(f"invalid bitbucket URI: {uri} — expected bitbucket://server/PROJECT/repo/refs/branch/path")
    server = parsed.netloc
    project = parts[0]
    repo = parts[1]
    # parts[2] == "refs"
    ref = parts[3]
    path = "/".join(parts[4:]) if len(parts) > 4 else ""
    return server, project, repo, ref, path


def _http_get(url: str, token: str, ca_bundle: str | None, client_cert: str | None) -> str:
    """HTTP GET with Bitbucket auth."""
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    if ca_bundle:
        ctx.load_verify_locations(ca_bundle)
    if client_cert:
        ctx.load_cert_chain(client_cert)

    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return resp.read().decode("utf-8")
