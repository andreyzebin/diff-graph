"""
Eager local-repo mode for `_run_with_dispatcher`.

When cli.py is invoked with --agent + --repo + --base + --source but no
--pr-url (the path unit-fixture investigator scenarios take when they
don't carry a `pr_state` block), we must NOT plumb the agent through
the lazy-clone path. The lazy path calls `fetch_pr(pr_url, ...)`
which itself calls `parse_pr_url("")` — that raises `ValueError`, the
agent runner wraps it as `"error: Cannot find 'projects' segment in
PR URL: "`, and every subsequent `diff_*` tool call returns the same
string. The investigator then spends its whole budget seeing an
empty world and lands at `done(confidence="low")` with nothing
concrete to report. Plan looks "finished" in the queue but that
scenario's score is junk.

These tests pin the eager-mode contract:

  1. With --repo + --base + --source and no --pr-url, the ctx
     comes back `_initialized=True` with diff_text / base_ref /
     source_ref / repo_path pre-populated.
  2. The first domain tool call therefore does NOT trigger
     `parse_pr_url` (which would error on the empty PR URL).
  3. With --pr-url and no local refs, we stay on the existing
     lazy path (unchanged contract for the PR-review flow).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def tiny_repo():
    """Two-commit git repo so `base..source` produces a non-empty diff."""
    repo = tempfile.mkdtemp(prefix="eager-ctx-")
    _git(["init"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (Path(repo) / "a.txt").write_text("hello\n")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-m", "base"], repo)
    base = _git(["rev-parse", "HEAD"], repo)
    (Path(repo) / "a.txt").write_text("hello\nworld\n")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-m", "source"], repo)
    source = _git(["rev-parse", "HEAD"], repo)
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


class _Stop(RuntimeError):
    """Sentinel — stops `_run_with_dispatcher` right after ctx is
    built so the test can inspect ctx without running an LLM."""


def _run(**kwargs):
    """Invoke `_run_with_dispatcher`, capture the ctx that gets passed
    to `register_diffgraph_tools`, then bail out via `_Stop`."""
    import contextlib
    captured: dict = {}

    def _capture_register(_registry, ctx):
        captured["ctx"] = ctx

    def _stop(*_a, **_kw):
        raise _Stop("ctx collected")

    patches = [
        patch("diffgraph.orchestra_tools.register_diffgraph_tools",
              side_effect=_capture_register),
        patch("cli._make_llm_client", side_effect=_stop),
        # Silence PR-side calls that run before ctx assembly.
        patch("diffgraph.bitbucket.get_pr_info", return_value={}),
        patch("diffgraph.bitbucket.get_pr_comments", return_value=[]),
        patch("diffgraph.bitbucket.get_comment_thread",
              return_value="(no thread)"),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        import cli
        try:
            cli._run_with_dispatcher(
                message="",
                comment_id=0,
                llm_cfg={"provider": "deepseek"},
                review_cfg={"bot_user": "test-bot"},
                effective_model="x",
                prompts=None,
                agent_name="investigator",
                **kwargs,
            )
        except _Stop:
            pass
    assert "ctx" in captured, "ctx was never built"
    return captured["ctx"]


class TestEagerLocalCtx:
    def test_initialized_when_repo_base_source_provided(self, tiny_repo):
        repo, base, source = tiny_repo
        ctx = _run(pr_url="", repo=repo, base=base, source=source)
        assert ctx._initialized is True, \
            "eager mode must mark ctx initialized so _ensure() is a no-op"
        assert ctx._init_fn is None, \
            "no lazy init fn should be attached in eager mode"

    def test_diff_and_refs_populated(self, tiny_repo):
        repo, base, source = tiny_repo
        ctx = _run(pr_url="", repo=repo, base=base, source=source)
        assert ctx.repo_path == str(Path(repo).resolve())
        assert ctx.base_ref == base
        assert ctx.source_ref == source
        assert ctx.diff_text, "eager mode must populate diff_text via git diff"
        assert "world" in ctx.diff_text, \
            f"diff_text should contain the added line:\n{ctx.diff_text}"
        assert ctx.diff_result.changed_files, \
            "parse_diff should pick up the changed file"

    def test_ensure_repo_is_noop_in_eager_mode(self, tiny_repo, monkeypatch):
        """If something tries to fetch_pr(), the test fails. ctx.ensure_repo()
        must NOT touch the PR plumbing."""
        repo, base, source = tiny_repo

        # Boobytrap: if anything calls fetch_pr or parse_pr_url, raise loudly.
        def _no_pr(*a, **kw):
            raise AssertionError(
                "eager-mode ctx must not call PR fetch / URL parse"
            )
        monkeypatch.setattr("diffgraph.bitbucket.fetch_pr", _no_pr)
        monkeypatch.setattr("diffgraph.bitbucket.parse_pr_url", _no_pr)

        ctx = _run(pr_url="", repo=repo, base=base, source=source)
        # The whole point — calling ensure_repo must NOT trip the booby trap.
        ctx.ensure_repo()


class TestLazyPathUnchanged:
    def test_lazy_when_pr_url_set(self, tiny_repo):
        """With --pr-url, the existing lazy path stays in force regardless
        of whether --repo etc. are also passed (the lazy fetch wins —
        cloning gives us the canonical base/source tuple anyway)."""
        ctx = _run(pr_url="https://bb/projects/X/repos/Y/pull-requests/1",
                   repo="", base="", source="")
        assert ctx._initialized is False
        assert ctx._init_fn is not None
        assert ctx._pr_url.endswith("/pull-requests/1")

    def test_lazy_when_local_refs_incomplete(self, tiny_repo):
        """Eager mode requires ALL of repo + base + source. Missing any
        one ⇒ stay on the lazy path (caller might have a fallback set
        of refs we don't know about)."""
        repo, base, _source = tiny_repo
        ctx = _run(pr_url="", repo=repo, base=base, source="")  # no source
        assert ctx._initialized is False
        assert ctx._init_fn is not None
