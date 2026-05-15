"""URI utilities for cross-repo addressing (§10.4).

URI shape: `bitbucket://<handle>/<project>/<repo>` — three hierarchical
levels (handle / project / repo). 1, 2, or 3 segments are all valid
addresses; tools validate the level they need:

  - `diff_*` and `pr_get` require a leaf URI (3 segments).
  - `pr_list` and `repo_list` accept any level.

`<handle>` is a logical name (e.g. "default", "internal") from a
`bitbucket_servers:` block in `config.local.yaml`, mirroring how
`jira_servers:` works. The handle resolves to a Bitbucket Server's
base URL + credentials. Hostnames are not used in the URI — handles
let references stay stable across deployments.

The `"default"` literal. Every `repo=` and `pr=` parameter on a tool
accepts the literal `"default"`, resolved at tool entry to the current
PR's URI / id from `ctx`. Omitting the param is equivalent. In
Phase B agents pass only `"default"`, so production behaviour is
unchanged; Phase C onwards exercises real URIs / PR ids in isolated
scenarios.

For Phase B the registry side is intentionally minimal — there is no
multi-server config parser yet, and the only handle resolved is
`default` (the current PR's server). Phase B tools accept arbitrary
URIs syntactically but reject non-default values at the handler with a
clear "Phase C" message; Phase C wires real cross-repo dispatch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


DEFAULT = "default"

# bitbucket://<handle>[/<project>[/<repo>]]
_SCHEME = "bitbucket://"
_BITBUCKET_PR_URL_RE = re.compile(
    r"/projects/(?P<project>[^/]+)/repos/(?P<repo>[^/]+)"
    r"(?:/pull-requests/(?P<pr>\d+))?"
)


@dataclass(frozen=True)
class RepoURI:
    """Parsed `bitbucket://<handle>[/<project>[/<repo>]]` address."""
    handle: str
    project: Optional[str] = None
    repo: Optional[str] = None

    @property
    def level(self) -> str:
        """One of: "server" (1 seg), "project" (2 seg), "repo" (3 seg, leaf)."""
        if self.repo is not None:
            return "repo"
        if self.project is not None:
            return "project"
        return "server"

    def is_leaf(self) -> bool:
        return self.level == "repo"

    def __str__(self) -> str:
        parts = [self.handle]
        if self.project is not None:
            parts.append(self.project)
        if self.repo is not None:
            parts.append(self.repo)
        return _SCHEME + "/".join(parts)


def parse_repo_uri(uri: str) -> RepoURI:
    """Parse a `bitbucket://...` URI.

    Raises ValueError on malformed input: wrong scheme, empty segments,
    >3 segments. Caller is responsible for validating the level matches
    the tool's expectation (e.g. `pr_get` needs leaf; `repo_list`
    accepts any).
    """
    if not isinstance(uri, str):
        raise ValueError(f"repo URI must be a string, got {type(uri).__name__}")
    if not uri.startswith(_SCHEME):
        raise ValueError(
            f"repo URI must start with '{_SCHEME}' — got {uri!r}"
        )
    rest = uri[len(_SCHEME):].rstrip("/")
    if not rest:
        raise ValueError(f"repo URI has no handle: {uri!r}")
    parts = rest.split("/")
    if len(parts) > 3:
        raise ValueError(
            f"repo URI has >3 path segments (handle/project/repo): {uri!r}"
        )
    if any(not p for p in parts):
        raise ValueError(f"repo URI has empty segment: {uri!r}")
    return RepoURI(
        handle=parts[0],
        project=parts[1] if len(parts) >= 2 else None,
        repo=parts[2] if len(parts) >= 3 else None,
    )


def is_default_literal(value: Optional[str]) -> bool:
    """True when the parameter value means "use the current context's
    default" — that is the `"default"` literal or an empty/omitted
    value. Resolves uniformly across `repo=` and `pr=` parameters."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return value == "" or value == DEFAULT


def ctx_repo_uri_from_pr_url(pr_url: str, *, handle: str = DEFAULT) -> Optional[RepoURI]:
    """Best-effort: derive a leaf URI from a Bitbucket PR URL.

    Returns None when the URL doesn't match Bitbucket's pattern (e.g.
    `fake://` test PRs, local repo runs without a real URL). In that
    case the caller should treat the current context as having no
    addressable URI — non-default repo values then fail the
    matches-current check below.
    """
    if not isinstance(pr_url, str) or not pr_url:
        return None
    m = _BITBUCKET_PR_URL_RE.search(pr_url)
    if not m:
        return None
    return RepoURI(handle=handle, project=m.group("project"), repo=m.group("repo"))


def ctx_pr_id_from_pr_url(pr_url: str) -> Optional[str]:
    """Best-effort: extract `<id>` from a Bitbucket PR URL ending in
    `/pull-requests/<id>`. Returns None when not matched."""
    if not isinstance(pr_url, str) or not pr_url:
        return None
    m = _BITBUCKET_PR_URL_RE.search(pr_url)
    if not m:
        return None
    return m.group("pr")


def resolve_repo_param(
    value: Optional[str],
    *,
    ctx_uri: Optional[RepoURI],
) -> tuple[Optional[RepoURI], Optional[str]]:
    """Resolve a `repo=` parameter to (resolved_uri, error_message).

    Phase B contract — see §10.6 tension 5:

      - Default (literal `"default"`, empty, or omitted) → returns
        `(ctx_uri, None)`. `ctx_uri` may itself be None (fake / local
        run); callers that need a real URI should treat that as "no
        cross-repo, use existing in-context behaviour".
      - Explicit URI matching `ctx_uri` → tolerated silently, returns
        `(parsed, None)`. (§10.6: "implementation tolerates
        `repo=<current's-actual-URI>` as silently equivalent.")
      - Explicit URI not matching ctx_uri → returns `(parsed, "<Phase B
        message>")`. Caller surfaces the message; Phase C swaps this
        gate for actual cross-repo dispatch.
      - Malformed URI → returns `(None, "invalid repo URI: ...")`.
    """
    if is_default_literal(value):
        return ctx_uri, None
    try:
        parsed = parse_repo_uri(value)  # type: ignore[arg-type]
    except ValueError as exc:
        return None, f"(invalid repo URI: {exc})"
    if ctx_uri is not None and str(parsed) == str(ctx_uri):
        return parsed, None
    return parsed, _PHASE_B_REPO_MSG


def resolve_pr_param(
    value: Optional[str],
    *,
    ctx_pr: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a `pr=` parameter to (resolved_id, error_message).
    Same Phase B contract as `resolve_repo_param`: default → context;
    matching value → tolerated; non-matching → Phase B message."""
    if is_default_literal(value):
        return ctx_pr, None
    # Accept integer-shaped strings only — anything else is malformed.
    if not isinstance(value, str) or not value.strip().isdigit():
        return None, f"(invalid pr id: {value!r})"
    if ctx_pr is not None and value == ctx_pr:
        return value, None
    return value, _PHASE_B_PR_MSG


_PHASE_B_REPO_MSG = (
    "(non-default repo not exercised in Phase B — pass `repo=\"default\"` "
    "or omit; cross-repo lookups arrive in Phase C isolated scenarios)"
)

_PHASE_B_PR_MSG = (
    "(non-default pr not exercised in Phase B — pass `pr=\"default\"` "
    "or omit; cross-PR lookups arrive in Phase C isolated scenarios)"
)
