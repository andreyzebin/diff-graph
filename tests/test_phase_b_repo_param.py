"""Phase B: `repo=` / `pr=` parameters across the tool surface (§10.8).

Phase B wires the cross-repo plumbing without exercising it. The
contract these tests pin:

  - Every tool that gained the parameters accepts `repo="default"` and
    `pr="default"` without breaking existing behaviour.
  - Schema-side: the new params show up in `parameters.properties`
    but NOT in `required` — agents that omit them dispatch fine.
  - Handler-side: a non-default URI returns the Phase B message. An
    explicit URI matching the current context is silently accepted
    (§10.6 — tolerates `repo=<current's-actual-URI>` as equivalent
    to `"default"`).
  - The three NEW tools (`pr_get`, `pr_list`, `repo_list`) are
    registered, return current-PR-scope data for default, and surface
    the Phase B message for non-default URIs. Real cross-repo
    enumeration is Phase C.

These tests use a stub ctx — Phase B's gate runs BEFORE any VFS
materialisation or Bitbucket API call, so no real git repo is needed
to exercise it.
"""
from __future__ import annotations

import pytest

from diffgraph.orchestra_tools import register_diffgraph_tools
from orchestra import ToolRegistry


# A realistic Bitbucket PR URL — matches the
# /projects/<P>/repos/<R>/pull-requests/<id> shape that the URI
# parser turns into bitbucket://default/ORD/orderflow + pr=1630.
REAL_PR_URL = (
    "https://bitbucket.example.com/projects/ORD/repos/orderflow/"
    "pull-requests/1630"
)
REAL_PR_URI = "bitbucket://default/ORD/orderflow"
REAL_PR_ID = "1630"


@pytest.fixture
def ctx_and_registry():
    """Stub ctx with a real-shape PR URL — enough for the Phase B gate
    to extract a current-repo URI and PR id from `_pr_url`."""
    from diffgraph.orchestrator import _Ctx, ReviewContext
    from diffgraph.diff_parser import DiffResult

    ctx = _Ctx(
        diff_text="",
        diff_result=DiffResult(files={}, changed_files=[], changed_lines={}),
        repo_path="/tmp/_phase_b_stub",
        existing_comments=[],
        review_context=ReviewContext(),
        base_ref="", source_ref="",
        _pr_url=REAL_PR_URL,
        _initialized=True,
    )
    reg = ToolRegistry()
    register_diffgraph_tools(reg, ctx)
    return ctx, reg


# ── Schema contract: params present, NOT required ─────────────────

class TestSchemaShape:
    """`repo=` / `pr=` must be optional — agents that omit them today
    keep working unchanged."""

    @pytest.mark.parametrize("tool_name", [
        "diff_list_files", "diff_read_file", "diff_outline", "diff_search",
    ])
    def test_diff_star_gains_repo_optional(self, ctx_and_registry, tool_name):
        _, reg = ctx_and_registry
        params = reg._tools[tool_name].parameters
        assert "repo" in params["properties"], f"{tool_name} missing repo"
        assert "repo" not in params.get("required", []), (
            f"{tool_name} must NOT mark repo as required"
        )

    @pytest.mark.parametrize("tool_name", [
        "pr_list_threads", "pr_read_thread", "pr_read_comment", "pr_post_comment",
    ])
    def test_pr_star_gains_repo_and_pr_optional(self, ctx_and_registry, tool_name):
        _, reg = ctx_and_registry
        params = reg._tools[tool_name].parameters
        assert "repo" in params["properties"]
        assert "pr" in params["properties"]
        req = params.get("required", [])
        assert "repo" not in req and "pr" not in req

    def test_new_tools_registered_with_no_required_params(self, ctx_and_registry):
        """`pr_get` / `pr_list` / `repo_list` are net-new tools. The
        agent should be able to call any of them with no arguments
        (they default to the current scope)."""
        _, reg = ctx_and_registry
        for tool_name in ("pr_get", "pr_list", "repo_list"):
            assert tool_name in reg._tools, f"{tool_name} not registered"
            params = reg._tools[tool_name].parameters
            assert params.get("required", []) == [], (
                f"{tool_name} must accept zero-arg calls"
            )


# ── Phase B gate on existing tools ─────────────────────────────────

class TestPhaseGateOnDiffStar:
    """`diff_*` tools — the gate runs before any VFS access, so we
    can probe it without a real git repo."""

    def test_non_default_repo_returns_phase_c_msg(self, ctx_and_registry):
        """diff_* tools now consult FakeBitbucket's
        cross_source_repos (Phase C entry-point) for non-default
        URIs. Without a fixture entry → clear "not available"
        message naming the missing URI — not the old Phase B
        rejection. The Phase B label remains accurate as a concept
        for tools that still bounce non-defaults (none of the ones
        we've migrated)."""
        _, reg = ctx_and_registry
        out = reg.dispatch("diff_list_files",
                            {"repo": "bitbucket://default/PROJ/shared-lib"})
        assert isinstance(out, str)
        assert "cross-source" in out
        assert "shared-lib" in out

    def test_malformed_repo_returns_invalid_uri_error(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("diff_list_files", {"repo": "not-a-uri"})
        assert isinstance(out, str)
        assert "invalid repo URI" in out

    def test_explicit_matching_uri_silently_accepted(self, ctx_and_registry):
        """A model that emits `repo=<current's URI>` instead of
        `"default"` must pass the gate — tolerated equivalence per
        §10.6. We can tell the gate passed by what comes back: a
        non-error string from the underlying handler (which may itself
        be empty/short on this stub ctx, but is NOT the Phase B message)."""
        _, reg = ctx_and_registry
        out = reg.dispatch("diff_list_files", {"repo": REAL_PR_URI})
        assert "Phase B" not in str(out), out
        assert "invalid repo URI" not in str(out), out


class TestPhaseGateOnPrStar:
    """`pr_*` tools — same gate, with `pr=` also checked."""

    def test_non_default_pr_returns_phase_c_msg(self, ctx_and_registry):
        """pr_list_threads now consults FakeBitbucket's
        cross_source_threads (Phase C entry-point) for non-default
        PR ids. Without a fixture entry → clear "not available"
        message that names the missing `<uri>:<pr>` key."""
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_list_threads", {"pr": "9999"})
        assert isinstance(out, str)
        assert "cross-source" in out
        assert "9999" in out

    def test_matching_pr_id_silently_accepted(self, ctx_and_registry):
        """`pr="1630"` (matching ctx's PR) is accepted — the gate
        falls through to the underlying handler. The handler returns
        whatever `pr_list_threads` produces on an empty comment list
        (a string, not the Phase B rejection)."""
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_list_threads", {"pr": REAL_PR_ID})
        assert "Phase B" not in str(out)

    def test_post_comment_rejects_non_default_repo(self, ctx_and_registry):
        """`pr_post_comment` returns a dict — the rejection comes
        through the same `_phase_b_gate` but is wrapped as a dict
        with status=error so the post-call flow can branch on it."""
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_post_comment", {
            "text": "x",
            "repo": "bitbucket://default/PROJ/shared-lib",
        })
        assert isinstance(out, dict)
        assert out["status"] == "error"
        assert "Phase B" in out["message"]


# ── New tools (pr_get / pr_list / repo_list) ───────────────────────

class TestNewTools:
    """`pr_get` / `pr_list` / `repo_list` registered in Phase B.
    For default scope they return current-PR data (lightweight
    plumbing); non-default surfaces the Phase B message — the real
    Bitbucket API enumeration arrives in Phase C scenarios."""

    def test_pr_get_default_returns_current_pr(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_get", {})
        assert isinstance(out, dict)
        assert out["uri"] == REAL_PR_URI
        assert out["pr"] == REAL_PR_ID
        assert out["pr_url"] == REAL_PR_URL
        assert "error" not in out

    def test_pr_get_non_default_unknown_returns_phase_c_error(self, ctx_and_registry):
        """pr_get now consults FakeBitbucket.cross_source_pr_meta for
        non-default URIs. Without a fixture entry, it returns a clear
        Phase-C "not configured" message — not the old Phase B
        rejection. The Phase C entry-point shape is locked in even
        though the in-fixture path is exercised separately
        (test_pr_get_cross_source_hits_fake)."""
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_get", {"pr": "9999"})
        assert isinstance(out, dict)
        assert "error" in out
        assert "cross-source" in out["error"]
        assert "9999" in out["error"]

    def test_pr_list_default_returns_current_pr_only(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_list", {})
        assert isinstance(out, list)
        # Phase B placeholder: just the current PR.
        assert len(out) == 1
        assert out[0]["uri"] == REAL_PR_URI
        assert out[0]["pr"] == REAL_PR_ID

    def test_pr_list_non_default_rejected(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch(
            "pr_list", {"repo": "bitbucket://default/PROJ/shared-lib"})
        assert isinstance(out, list)
        assert len(out) == 1
        assert "error" in out[0]
        # Phase C entry-point now consults FakeBitbucket — without
        # a fixture entry it returns the not-configured message.
        assert "cross-source" in out[0]["error"]
        assert "shared-lib" in out[0]["error"]

    def test_repo_list_default_returns_current_repo(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("repo_list", {})
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0]["uri"] == REAL_PR_URI

    def test_repo_list_non_default_returns_phase_c_msg(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch(
            "repo_list", {"repo": "bitbucket://internal/SOME/thing"})
        assert "cross-source" in out[0]["error"]


# ── Default-omission stays unchanged ───────────────────────────────

class TestOmittedParamsActAsDefault:
    """Most existing scenarios call tools without the new params at
    all. That has to keep working — omitted equals default equals
    current context."""

    def test_diff_list_files_no_repo_arg(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("diff_list_files", {})
        assert "Phase B" not in str(out)
        assert "invalid repo URI" not in str(out)

    def test_pr_list_threads_no_repo_no_pr(self, ctx_and_registry):
        _, reg = ctx_and_registry
        out = reg.dispatch("pr_list_threads", {})
        assert "Phase B" not in str(out)


# ── §10 Phase C entry-point: pr_get against fake cross-source ─────


class TestPrGetCrossSourceFake:
    """`pr_get(repo=<other-uri>, pr=<id>)` consults
    FakeBitbucket.cross_source_pr_meta. Fixture entry → meta
    returned; missing → Phase C "not configured" message. Production
    swaps a real BitbucketRegistry behind the same method shape."""

    def _install_fake_with_cross(self, monkeypatch, cross: dict):
        """Install a FakeBitbucket whose payload carries the given
        cross_source_prs map. The orchestra_tools' pr_get reaches
        the singleton via `bitbucket_fake._instance()`."""
        from diffgraph import bitbucket_fake as fb
        fake = fb.FakeBitbucket(payload={
            "pr_url": REAL_PR_URL,
            "repo_path": "/tmp/_x", "base_sha": "a", "source_sha": "b",
            "metadata": {}, "comments": [], "self_user": "bot",
            "cross_source_prs": cross,
        })
        fb.install(fake)
        # Idempotent teardown for the next test.
        monkeypatch.setattr("diffgraph.bitbucket_fake._instance",
                            lambda: fake)
        return fake

    def test_returns_meta_when_entry_present(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake_with_cross(monkeypatch, {
            "bitbucket://default/SHARED/lib:42": {
                "base_ref": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "source_ref": "feedfacefeedfacefeedfacefeedfacefeedface",
                "pr_url": "https://bitbucket.example.com/projects/SHARED/repos/lib/pull-requests/42",
                "metadata": {
                    "title": "Bump library to 2.3",
                    "state": "OPEN",
                    "author": "alice",
                },
            },
        })
        out = reg.dispatch("pr_get", {
            "repo": "bitbucket://default/SHARED/lib",
            "pr": "42",
        })
        assert "error" not in out, out
        assert out["uri"] == "bitbucket://default/SHARED/lib"
        assert out["pr"] == "42"
        assert out["base_ref"].startswith("deadbeef")
        assert out["title"] == "Bump library to 2.3"
        assert out["state"] == "OPEN"

    def test_missing_entry_returns_phase_c_msg(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake_with_cross(monkeypatch, {})
        out = reg.dispatch("pr_get", {
            "repo": "bitbucket://default/SHARED/lib",
            "pr": "99",
        })
        assert "error" in out
        assert "cross-source" in out["error"]
        assert "lib:99" in out["error"]

    def test_explicit_current_uri_silently_ok(self, monkeypatch, ctx_and_registry):
        """§10.6: passing the current PR's exact URI must be tolerated
        as silently equivalent to "default" — no cross-source lookup
        needed."""
        _, reg = ctx_and_registry
        self._install_fake_with_cross(monkeypatch, {})  # empty cross-source map
        out = reg.dispatch("pr_get", {
            "repo": REAL_PR_URI,
            "pr": REAL_PR_ID,
        })
        assert "error" not in out, out
        assert out["uri"] == REAL_PR_URI
        assert out["pr"] == REAL_PR_ID

    def test_invalid_uri_returns_parse_error(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake_with_cross(monkeypatch, {})
        out = reg.dispatch("pr_get", {
            "repo": "not-a-valid-uri",
            "pr": "1",
        })
        assert "error" in out
        assert "invalid repo URI" in out["error"]


# ── §10 Phase C: pr_list / repo_list cross-source fakes ───────────


class TestPrListRepoListCrossSourceFake:
    """`pr_list(repo=<uri>)` and `repo_list(repo=<uri>)` consult
    FakeBitbucket's cross-source maps with prefix matching: a
    project-level URI sees all leaf entries under it; a server-
    level URI sees everything. Production Phase C swaps in a real
    BitbucketRegistry behind the same method shape."""

    def _install_fake(self, monkeypatch, pr_list_map=None, repo_list_map=None):
        from diffgraph import bitbucket_fake as fb
        fake = fb.FakeBitbucket(payload={
            "pr_url": REAL_PR_URL,
            "repo_path": "/tmp/_x", "base_sha": "a", "source_sha": "b",
            "metadata": {}, "comments": [], "self_user": "bot",
            "cross_source_pr_list": pr_list_map or {},
            "cross_source_repo_list": repo_list_map or {},
        })
        fb.install(fake)
        monkeypatch.setattr("diffgraph.bitbucket_fake._instance",
                            lambda: fake)
        return fake

    def test_pr_list_leaf_exact_hit(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, pr_list_map={
            "bitbucket://default/SHARED/lib": [
                {"uri": "bitbucket://default/SHARED/lib", "pr": "42",
                 "title": "Bump lib", "state": "OPEN"},
                {"uri": "bitbucket://default/SHARED/lib", "pr": "43",
                 "title": "Refactor", "state": "MERGED"},
            ],
        })
        out = reg.dispatch("pr_list", {"repo": "bitbucket://default/SHARED/lib"})
        assert len(out) == 2
        assert out[0]["pr"] == "42"
        assert out[1]["title"] == "Refactor"

    def test_pr_list_project_level_aggregates(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, pr_list_map={
            "bitbucket://default/SHARED/lib":   [{"pr": "1"}],
            "bitbucket://default/SHARED/utils": [{"pr": "2"}, {"pr": "3"}],
            "bitbucket://default/OTHER/thing":  [{"pr": "99"}],  # OUTSIDE the project
        })
        out = reg.dispatch("pr_list", {"repo": "bitbucket://default/SHARED"})
        prs = sorted(d.get("pr", "") for d in out if "pr" in d)
        assert prs == ["1", "2", "3"]   # OTHER/thing's "99" excluded

    def test_pr_list_missing_returns_phase_c_msg(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, pr_list_map={})
        out = reg.dispatch("pr_list", {"repo": "bitbucket://default/SHARED/lib"})
        assert "error" in out[0]
        assert "cross-source" in out[0]["error"]

    def test_repo_list_server_level_aggregates(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, repo_list_map={
            "bitbucket://default/A/r1": [
                {"uri": "bitbucket://default/A/r1", "name": "r1"}],
            "bitbucket://default/B/r2": [
                {"uri": "bitbucket://default/B/r2", "name": "r2"}],
        })
        out = reg.dispatch("repo_list", {"repo": "bitbucket://default"})
        names = sorted(d.get("name", "") for d in out if "name" in d)
        assert names == ["r1", "r2"]

    def test_invalid_uri_returns_parse_error(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch)
        out = reg.dispatch("pr_list", {"repo": "not-a-uri"})
        assert "error" in out[0]
        assert "invalid repo URI" in out[0]["error"]


# ── §10 Phase C: pr_list_threads / pr_read_thread / pr_read_comment ──


class TestThreadsCrossSourceFake:
    """The three thread tools consult FakeBitbucket's
    cross_source_threads map when the caller passes a non-default
    URI/PR. Fixture hit → comments rendered through the same impl
    functions as the default-path; miss → Phase C message."""

    def _install_fake(self, monkeypatch, threads_map):
        from diffgraph import bitbucket_fake as fb
        fake = fb.FakeBitbucket(payload={
            "pr_url": REAL_PR_URL,
            "repo_path": "/tmp/_x", "base_sha": "a", "source_sha": "b",
            "metadata": {}, "comments": [], "self_user": "bot",
            "cross_source_threads": threads_map,
        })
        fb.install(fake)
        monkeypatch.setattr("diffgraph.bitbucket_fake._instance",
                            lambda: fake)
        return fake

    def test_pr_list_threads_cross_source_hit(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {
            "bitbucket://default/SHARED/lib:42": [
                {"id": 100, "parent_id": 0, "text": "Looks good to me",
                 "author": "alice", "anchor": None},
                {"id": 101, "parent_id": 100, "text": "lgtm",
                 "author": "bob", "anchor": None},
                {"id": 200, "parent_id": 0, "text": "What about the perf hit?",
                 "author": "carol", "anchor": None},
            ],
        })
        out = reg.dispatch("pr_list_threads", {
            "repo": "bitbucket://default/SHARED/lib", "pr": "42",
        })
        assert isinstance(out, str)
        assert "Phase B" not in out and "cross-source" not in out
        # Both root threads listed (id=100 and id=200).
        assert "100" in out and "200" in out

    def test_pr_read_thread_cross_source_walks_subtree(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {
            "bitbucket://default/SHARED/lib:42": [
                {"id": 100, "parent_id": 0, "text": "root",
                 "author": "alice", "anchor": None},
                {"id": 101, "parent_id": 100, "text": "reply",
                 "author": "bob", "anchor": None},
            ],
        })
        out = reg.dispatch("pr_read_thread", {
            "comment_id": 101,
            "repo": "bitbucket://default/SHARED/lib", "pr": "42",
        })
        # Reading the reply walks UP to the root and renders the
        # whole subtree, so both comments show in the output.
        assert "root" in out and "reply" in out

    def test_pr_read_comment_cross_source(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {
            "bitbucket://default/SHARED/lib:42": [
                {"id": 100, "parent_id": 0,
                 "text": "the entire comment body here",
                 "author": "alice", "anchor": None},
            ],
        })
        out = reg.dispatch("pr_read_comment", {
            "comment_id": 100,
            "repo": "bitbucket://default/SHARED/lib", "pr": "42",
        })
        assert "entire comment body" in out

    def test_missing_entry_returns_phase_c_msg(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {})
        out = reg.dispatch("pr_list_threads", {
            "repo": "bitbucket://default/SHARED/lib", "pr": "42",
        })
        assert "cross-source" in out
        assert "lib:42" in out


# ── §10 Phase C: diff_* cross-source VFS via fake ──────────────────


def _make_tiny_git_repo(tmp_path, files_v1: dict, files_v2: dict) -> tuple[str, str, str]:
    """Build a 2-commit git repo at tmp_path and return
    (repo_path, base_sha, source_sha). v1 → v2 commit pair is
    everything diff_* / VFS materialization needs."""
    import subprocess
    repo = tmp_path / "x_repo"
    repo.mkdir()
    def _git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    for rel, content in files_v1.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git("add", ".")
    _git("commit", "-q", "-m", "v1")
    base = _git("rev-parse", "HEAD")
    for rel, content in files_v2.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git("add", ".")
    _git("commit", "-q", "-m", "v2")
    source = _git("rev-parse", "HEAD")
    return str(repo), base, source


class TestDiffStarCrossSourceFake:
    """diff_* tools materialize a VFS against the cross-source
    (repo_path, base_sha, source_sha) triple from FakeBitbucket's
    cross_source_repos map. Production Phase C will swap a real
    BitbucketRegistry behind the same method shape."""

    def _install_fake(self, monkeypatch, repos_map):
        from diffgraph import bitbucket_fake as fb
        fake = fb.FakeBitbucket(payload={
            "pr_url": REAL_PR_URL,
            "repo_path": "/tmp/_unused", "base_sha": "a", "source_sha": "b",
            "metadata": {}, "comments": [], "self_user": "bot",
            "cross_source_repos": repos_map,
        })
        fb.install(fake)
        monkeypatch.setattr("diffgraph.bitbucket_fake._instance",
                            lambda: fake)
        return fake

    def test_diff_read_file_cross_source(self, monkeypatch, tmp_path, ctx_and_registry):
        _, reg = ctx_and_registry
        repo_path, base, source = _make_tiny_git_repo(
            tmp_path,
            files_v1={"src/lib.py": "def hello():\n    return 'old'\n"},
            files_v2={"src/lib.py": "def hello():\n    return 'new'\n"},
        )
        self._install_fake(monkeypatch, {
            "bitbucket://default/SHARED/lib": {
                "repo_path": repo_path, "base_sha": base, "source_sha": source,
            },
        })
        out = reg.dispatch("diff_read_file", {
            "path": "src/lib.py",
            "repo": "bitbucket://default/SHARED/lib",
        })
        assert isinstance(out, str)
        assert "cross-source" not in out
        # Diff annotations should show the - and + lines.
        assert "old" in out and "new" in out

    def test_diff_list_files_cross_source(self, monkeypatch, tmp_path, ctx_and_registry):
        _, reg = ctx_and_registry
        repo_path, base, source = _make_tiny_git_repo(
            tmp_path,
            files_v1={"a.txt": "1\n", "b.txt": "x\n"},
            files_v2={"a.txt": "1\n", "b.txt": "y\n"},  # only b changed
        )
        self._install_fake(monkeypatch, {
            "bitbucket://default/SHARED/lib": {
                "repo_path": repo_path, "base_sha": base, "source_sha": source,
            },
        })
        out = reg.dispatch("diff_list_files", {
            "repo": "bitbucket://default/SHARED/lib",
            "changes_only": True,
        })
        assert "cross-source" not in out
        assert "b.txt" in out

    def test_missing_entry_returns_phase_c_msg(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {})
        out = reg.dispatch("diff_read_file", {
            "path": "anything.txt",
            "repo": "bitbucket://default/SHARED/lib",
        })
        assert "cross-source" in out
        assert "lib" in out

    def test_non_leaf_uri_rejected(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {})
        out = reg.dispatch("diff_list_files", {
            "repo": "bitbucket://default/SHARED",  # project level
        })
        assert "leaf URI" in out

    def test_invalid_uri_returns_parse_error(self, monkeypatch, ctx_and_registry):
        _, reg = ctx_and_registry
        self._install_fake(monkeypatch, {})
        out = reg.dispatch("diff_list_files", {"repo": "not-a-uri"})
        assert "invalid repo URI" in out
