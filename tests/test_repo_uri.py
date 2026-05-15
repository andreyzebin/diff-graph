"""URI parsing + Phase B parameter resolution for the cross-repo
toolset (§10.4 / §10.6).

The URI standard: `bitbucket://<handle>[/<project>[/<repo>]]` — three
hierarchical levels (handle / project / repo). `<handle>` is logical,
not a hostname.

Phase B contract for `repo=` / `pr=` on tools (see §10.6 tension 5):

  - `"default"` literal / empty / omitted → resolves to context.
  - Explicit URI matching the current PR's URI → tolerated silently
    (the implementation accepts `repo=<current's-actual-URI>` as
    equivalent to `"default"` so a model emitting either form works).
  - Anything else → returns a clear "Phase B" rejection message;
    Phase C scenarios replace this gate with real cross-repo dispatch.
"""
from __future__ import annotations

import pytest

from diffgraph.repo_uri import (
    DEFAULT,
    RepoURI,
    parse_repo_uri,
    is_default_literal,
    ctx_repo_uri_from_pr_url,
    ctx_pr_id_from_pr_url,
    resolve_repo_param,
    resolve_pr_param,
)


# ── parse_repo_uri ─────────────────────────────────────────────────

class TestParseRepoURI:
    """Three hierarchical levels are all valid addresses."""

    def test_server_level(self):
        """Handle-only URI = server-level. Used by `pr_list` /
        `repo_list` for cross-project enumeration."""
        u = parse_repo_uri("bitbucket://default")
        assert u == RepoURI(handle="default")
        assert u.level == "server"
        assert not u.is_leaf()

    def test_project_level(self):
        """Handle + project = project-level. `pr_list` at project
        level lists PRs across all the project's repos."""
        u = parse_repo_uri("bitbucket://default/ORD")
        assert u == RepoURI(handle="default", project="ORD")
        assert u.level == "project"

    def test_repo_level_leaf(self):
        """Handle + project + repo = leaf URI — what `diff_*` and
        `pr_get` require."""
        u = parse_repo_uri("bitbucket://default/ORD/orderflow")
        assert u == RepoURI(handle="default", project="ORD", repo="orderflow")
        assert u.level == "repo"
        assert u.is_leaf()

    def test_roundtrip(self):
        """str(parse(uri)) == uri at every level. Tools serialise the
        resolved URI into result payloads — must be stable."""
        for uri in [
            "bitbucket://default",
            "bitbucket://default/ORD",
            "bitbucket://default/ORD/orderflow",
            "bitbucket://internal/PROJ/some-repo",
        ]:
            assert str(parse_repo_uri(uri)) == uri

    def test_trailing_slash_tolerated(self):
        """`bitbucket://default/` is the same as `bitbucket://default`
        (REST-style trailing slash). One canonical parse."""
        assert parse_repo_uri("bitbucket://default/") == RepoURI(handle="default")
        assert parse_repo_uri("bitbucket://default/ORD/") == \
            RepoURI(handle="default", project="ORD")

    @pytest.mark.parametrize("bad", [
        "default/ORD/orderflow",                    # missing scheme
        "https://bitbucket.example.com/PROJ/repo",  # wrong scheme
        "bitbucket://",                             # no handle
        "bitbucket:///PROJ/repo",                   # empty handle
        "bitbucket://default/ORD/orderflow/extra",  # >3 segments
        "bitbucket://default//orderflow",           # empty middle segment
    ])
    def test_malformed_rejected(self, bad):
        """Bad URIs raise ValueError — caller surfaces it as a tool
        error rather than guessing the user's intent."""
        with pytest.raises(ValueError):
            parse_repo_uri(bad)

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            parse_repo_uri(None)  # type: ignore[arg-type]


# ── is_default_literal ─────────────────────────────────────────────

class TestIsDefaultLiteral:
    """The shape every `repo=` / `pr=` parameter resolves first."""

    @pytest.mark.parametrize("v", ["default", "", None])
    def test_default_shapes(self, v):
        assert is_default_literal(v) is True

    @pytest.mark.parametrize("v", [
        "bitbucket://default",
        "1630",
        "0",
        "DEFAULT",       # case-sensitive — only "default" qualifies
        " default ",     # caller hands a trimmed value
    ])
    def test_non_default(self, v):
        assert is_default_literal(v) is False


# ── ctx_repo_uri_from_pr_url / ctx_pr_id_from_pr_url ───────────────

class TestCtxExtraction:
    """Best-effort URL → URI parser. Production PR URLs match
    Bitbucket's `/projects/<P>/repos/<R>/pull-requests/<id>` shape;
    fake / local runs don't and return None — Phase B handler then
    treats any non-default param as a mismatch."""

    REAL_URL = (
        "https://bitbucket.example.com/projects/ORD/repos/orderflow/"
        "pull-requests/1630"
    )

    def test_extracts_uri_from_real_url(self):
        u = ctx_repo_uri_from_pr_url(self.REAL_URL)
        assert u == RepoURI(handle="default", project="ORD", repo="orderflow")

    def test_extracts_pr_id_from_real_url(self):
        assert ctx_pr_id_from_pr_url(self.REAL_URL) == "1630"

    def test_fake_url_returns_none(self):
        """`fake://...` test PRs don't have the Bitbucket shape —
        ctx URI is None. Non-default params then fail the match check."""
        assert ctx_repo_uri_from_pr_url("fake://test/pr/1") is None
        assert ctx_pr_id_from_pr_url("fake://test/pr/1") is None

    def test_empty_url_returns_none(self):
        assert ctx_repo_uri_from_pr_url("") is None
        assert ctx_pr_id_from_pr_url("") is None

    def test_browse_url_without_pr_id(self):
        """A repo browse URL has `/projects/<P>/repos/<R>` but no PR
        suffix — URI extractable, PR id is None."""
        url = "https://bitbucket.example.com/projects/ORD/repos/orderflow/browse"
        assert ctx_repo_uri_from_pr_url(url) == \
            RepoURI(handle="default", project="ORD", repo="orderflow")
        assert ctx_pr_id_from_pr_url(url) is None

    def test_handle_override(self):
        """The handle is hard-coded `default` in Phase B — Phase C
        will derive it from `bitbucket_servers:` config. The kwarg
        exists so the test infra can pass a non-default value early."""
        u = ctx_repo_uri_from_pr_url(self.REAL_URL, handle="internal")
        assert u.handle == "internal"


# ── resolve_repo_param ─────────────────────────────────────────────

class TestResolveRepoParam:
    """Phase B contract on the `repo=` parameter."""

    CTX = RepoURI(handle="default", project="ORD", repo="orderflow")

    def test_default_literal_returns_ctx(self):
        out, err = resolve_repo_param("default", ctx_uri=self.CTX)
        assert out == self.CTX
        assert err is None

    def test_empty_string_returns_ctx(self):
        out, err = resolve_repo_param("", ctx_uri=self.CTX)
        assert out == self.CTX
        assert err is None

    def test_none_returns_ctx(self):
        out, err = resolve_repo_param(None, ctx_uri=self.CTX)
        assert out == self.CTX
        assert err is None

    def test_explicit_uri_matching_ctx_silently_accepted(self):
        """§10.6: the implementation tolerates `repo=<current's-URI>`
        as equivalent to `"default"`. A model emitting either form
        gets through Phase B without rejection."""
        out, err = resolve_repo_param(
            "bitbucket://default/ORD/orderflow", ctx_uri=self.CTX)
        assert err is None
        assert out == self.CTX

    def test_explicit_uri_not_matching_ctx_rejected_with_phase_b_message(self):
        out, err = resolve_repo_param(
            "bitbucket://default/PROJ/shared-lib", ctx_uri=self.CTX)
        assert err is not None
        assert "Phase B" in err
        # The parsed URI is returned alongside the error so Phase C can
        # swap the gate for real dispatch without changing the call
        # site shape — the URI is already parsed.
        assert out == RepoURI(handle="default", project="PROJ", repo="shared-lib")

    def test_malformed_uri_returns_error(self):
        out, err = resolve_repo_param("not-a-uri", ctx_uri=self.CTX)
        assert err is not None
        assert "invalid repo URI" in err
        assert out is None

    def test_no_ctx_uri_any_explicit_value_rejected(self):
        """Fake / local run with no real PR URL: ctx_uri is None, so
        nothing matches and any non-default value gets the Phase B
        message. Default still passes through (returning None)."""
        out_default, err_default = resolve_repo_param("default", ctx_uri=None)
        assert err_default is None
        assert out_default is None
        out_explicit, err_explicit = resolve_repo_param(
            "bitbucket://default/ORD/orderflow", ctx_uri=None)
        assert err_explicit is not None and "Phase B" in err_explicit


# ── resolve_pr_param ───────────────────────────────────────────────

class TestResolvePrParam:
    """Same Phase B contract on the `pr=` parameter."""

    def test_default_returns_ctx(self):
        out, err = resolve_pr_param("default", ctx_pr="1630")
        assert out == "1630"
        assert err is None

    def test_explicit_matching_silently_accepted(self):
        out, err = resolve_pr_param("1630", ctx_pr="1630")
        assert err is None
        assert out == "1630"

    def test_explicit_non_matching_rejected(self):
        out, err = resolve_pr_param("1631", ctx_pr="1630")
        assert err is not None
        assert "Phase B" in err

    def test_non_digit_value_rejected_as_invalid(self):
        """PR ids are digit strings. A non-digit value is a model
        mistake and surfaces as 'invalid pr id', not the Phase B
        rejection — different class of error, different message."""
        out, err = resolve_pr_param("HEAD", ctx_pr="1630")
        assert err is not None
        assert "invalid pr id" in err

    def test_no_ctx_pr_default_still_passes(self):
        out, err = resolve_pr_param("default", ctx_pr=None)
        assert err is None
        assert out is None
