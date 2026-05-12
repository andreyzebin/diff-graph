"""`react_to_comment` graceful 404 path.

Bitbucket Server doesn't expose a public reactions REST endpoint —
calls to `/comments/<id>/reactions/<emoticon>` return 404. We catch
that specifically in `bitbucket.react_to_pr_comment` and raise
`ReactionsUnsupportedError`; the orchestra tool wrapper translates
it into a structured `{"status": "unsupported", ...}` result so the
agent knows to fall back to `post_comment(parent_id=...)` instead of
retrying with a different emoticon hoping it'll succeed.

Pin: the result must contain `status="unsupported"` and a `hint`
field mentioning `post_comment` so the agent has a concrete
fallback in its tool_result.

`react_to_pr_comment` requires a bearer token (read from env when
not passed); the `_env_token` fixture sets
`BITBUCKET_SERVER_BEARER_TOKEN` so we exercise the 404 branch rather
than the early "no token" guard.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from diffgraph.bitbucket import (
    ReactionsUnsupportedError,
    react_to_pr_comment,
)


@pytest.fixture(autouse=True)
def _env_token(monkeypatch):
    """`react_to_pr_comment` reads the bearer token from env when
    not passed explicitly; without one it raises ValueError BEFORE
    hitting our 404 catch. Set a dummy token so every test in this
    module exercises the HTTP branch."""
    monkeypatch.setenv("BITBUCKET_SERVER_BEARER_TOKEN", "test-token")


def _mk_404(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url, code=404, msg="Not Found",
        hdrs=None, fp=None,
    )


def _mk_500(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url, code=500, msg="Internal Server Error",
        hdrs=None, fp=None,
    )


class TestReactUnsupported:

    def test_404_raises_reactions_unsupported(self):
        """When the reactions endpoint returns 404, we re-wrap as
        the specific subclass so callers can catch + degrade."""
        pr_url = "https://srv/bitbucket-ci/projects/P/repos/R/pull-requests/1"
        with patch(
            "diffgraph.bitbucket._api_post",
            side_effect=_mk_404("https://srv/.../reactions/thumbs_up"),
        ):
            with pytest.raises(ReactionsUnsupportedError) as excinfo:
                react_to_pr_comment(
                    pr_url, 42, "thumbs_up",
                    token="fake-token",
                )
            # Message must give the agent something actionable —
            # point at the alternative path.
            assert "post_comment" in str(excinfo.value)
            assert "42" in str(excinfo.value)  # the comment id

    def test_500_passes_through_as_generic_httperror(self):
        """Non-404 errors are NOT reactions-unsupported; the original
        HTTPError must propagate so generic retry / logging logic
        handles them."""
        pr_url = "https://srv/bitbucket-ci/projects/P/repos/R/pull-requests/1"
        with patch(
            "diffgraph.bitbucket._api_post",
            side_effect=_mk_500("https://srv/.../reactions/thumbs_up"),
        ):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                react_to_pr_comment(
                    pr_url, 42, "thumbs_up",
                    token="fake-token",
                )
            # And it must NOT be reclassified as the
            # reactions-specific subtype.
            assert not isinstance(excinfo.value, ReactionsUnsupportedError)
            assert excinfo.value.code == 500


class TestReactToolResult:
    """Orchestra tool wrapper around `react_to_pr_comment`. Pinned
    here because the structured `{"status": "unsupported", "hint":
    ...}` shape is what the agent's reflect+plan loop reads from the
    tool_result — change the keys and prompts that rely on the
    `hint` field break silently."""

    def _registry_with_react(self):
        from diffgraph.orchestrator import _Ctx, ReviewContext
        from diffgraph.diff_parser import DiffResult
        from diffgraph.orchestra_tools import register_diffgraph_tools
        from orchestra import ToolRegistry
        ctx = _Ctx(
            diff_text="",
            diff_result=DiffResult(files={}, changed_files=[], changed_lines={}),
            repo_path="/tmp", existing_comments=[],
            review_context=ReviewContext(),
            base_ref="", source_ref="",
            _pr_url="https://srv/bitbucket-ci/projects/P/repos/R/pull-requests/1",
            _initialized=True,
        )
        reg = ToolRegistry()
        register_diffgraph_tools(reg, ctx)
        return reg

    def test_unsupported_status_when_server_returns_404(self):
        reg = self._registry_with_react()
        with patch(
            "diffgraph.bitbucket._api_post",
            side_effect=_mk_404("https://srv/.../reactions/thumbs_up"),
        ):
            out = reg.dispatch("react_to_comment", {
                "comment_id": 42,
                "emoticon": "thumbs_up",
            })
        assert isinstance(out, dict)
        assert out.get("status") == "unsupported"
        assert out.get("comment_id") == 42
        assert "post_comment" in out.get("hint", "")
        # Message carries the underlying explanation too.
        assert "404" in out.get("message", "")

    def test_other_errors_stay_as_status_error(self):
        reg = self._registry_with_react()
        with patch(
            "diffgraph.bitbucket._api_post",
            side_effect=_mk_500("https://srv/.../reactions/thumbs_up"),
        ):
            out = reg.dispatch("react_to_comment", {
                "comment_id": 42,
                "emoticon": "thumbs_up",
            })
        assert out.get("status") == "error"
        # Not the unsupported sentinel.
        assert "hint" not in out
