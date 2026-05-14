"""
Integration tests for `diffgraph.bitbucket_fake`.

The fake module is the production seam between the unit-tier bench
runner and diff-graph's cli. These tests pin the FakeBitbucket class
contract (read side, write side, sink behaviour) AND the module-level
singleton routing — the path real cli.py code uses after
`diffgraph/bitbucket.py` rebinds its exports here.

Coverage shape:
  • read side: fetch_pr against a real local git repo + comment thread
    rendering through parent_id chains.
  • write side: every action records to the configured sink(s).
  • routing: install() / reset() / env-init / two-instance isolation.

A real tmp git repo is used for fetch_pr so we exercise the actual
`git diff base..source` subprocess, not a mock — that's the whole
point of "integration" vs. unit here.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from diffgraph import bitbucket_fake as bf


# ─── repo fixture ─────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def two_commit_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A tmp git repo with two commits. Returns (repo_path, base_sha,
    source_sha). The diff between them touches one file so fetch_pr
    has something to render."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name",  "tester")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "README.md").write_text("hello\nworld\n")
    (repo / "new.txt").write_text("a new file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")
    source = _git(repo, "rev-parse", "HEAD")

    return repo, base, source


@pytest.fixture(autouse=True)
def reset_singleton():
    """Every test starts with a clean module-level singleton — no
    state from earlier tests, no env-var leakage either way."""
    bf.reset()
    saved = {k: os.environ.pop(k, None)
             for k in ("DIFFGRAPH_FAKE_PR_FILE", "DIFFGRAPH_FAKE_PR_SINK")}
    try:
        yield
    finally:
        bf.reset()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ─── parse_pr_url (pure parser, no state) ─────────────────────────────────


class TestParsePRURL:
    def test_real_bitbucket_shape(self):
        srv, proj, repo, pr_id = bf.parse_pr_url(
            "https://bitbucket.example.com/projects/PROJ/repos/myrepo/pull-requests/42"
        )
        assert srv == "https://bitbucket.example.com"
        assert (proj, repo, pr_id) == ("PROJ", "myrepo", 42)

    def test_fake_url_without_projects_segment_returns_fallback(self):
        """The fake-shape URL used by bench unit fixtures
        (`fake://orderflow/UNIT/repos/.../pull-requests/N`) doesn't
        carry a `projects/` segment, so the parser hits the fallback —
        same as any unparseable URL. This is fine in practice because
        FakeBitbucket's methods don't actually consume the parsed
        project/repo/pr_id (they go off the in-memory payload). Pinning
        the behaviour here so a "fix" that silently changes the
        fallback tuple doesn't break the bench wiring."""
        srv, proj, repo, pr_id = bf.parse_pr_url(
            "fake://orderflow/UNIT/repos/orderflow/pull-requests/301"
        )
        assert (proj, repo, pr_id) == ("FAKE", "fake-repo", 0)

    def test_malformed_url_returns_fallback_not_raises(self):
        srv, proj, repo, pr_id = bf.parse_pr_url("not even a url")
        assert proj == "FAKE" and repo == "fake-repo" and pr_id == 0


# ─── read side (FakeBitbucket class) ──────────────────────────────────────


class TestReadSide:
    def test_fetch_pr_returns_real_git_diff(self, two_commit_repo):
        repo, base, source = two_commit_repo
        fake = bf.FakeBitbucket({
            "pr_url": "fake://x/Y/repos/Z/pull-requests/1",
            "repo_path": str(repo),
            "base_sha": base,
            "source_sha": source,
            "metadata": {"title": "feature"},
        })
        diff, cwd, cleanup, meta = fake.fetch_pr("fake://x/Y/repos/Z/pull-requests/1")
        # We added one line to README and a new file — diff must mention both.
        assert "README.md" in diff
        assert "new.txt"   in diff
        assert "+world"    in diff
        assert cwd == str(repo)
        # Cleanup is a no-op (runner owns the temp clone) but must be callable.
        cleanup()
        # Metadata flows through + base_ref/source_ref default-filled.
        assert meta["title"]      == "feature"
        assert meta["base_ref"]   == base
        assert meta["source_ref"] == source

    def test_fetch_pr_raises_on_incomplete_payload(self):
        fake = bf.FakeBitbucket({"pr_url": "x", "repo_path": "", "base_sha": "", "source_sha": ""})
        with pytest.raises(RuntimeError, match="missing repo_path"):
            fake.fetch_pr("x")

    def test_fetch_pr_status_callback_invoked(self, two_commit_repo, capsys):
        repo, base, source = two_commit_repo
        fake = bf.FakeBitbucket({
            "pr_url": "x", "repo_path": str(repo),
            "base_sha": base, "source_sha": source, "metadata": {},
        })
        statuses = []
        fake.fetch_pr("x", on_status=lambda s: statuses.append(s))
        assert any("fake fetch_pr" in s for s in statuses)

    def test_get_pr_info_supplies_base_source_ref_defaults(self):
        fake = bf.FakeBitbucket({
            "pr_url": "x", "repo_path": "/x", "base_sha": "B0", "source_sha": "S0",
            "metadata": {"title": "t"},
        })
        info = fake.get_pr_info("x")
        assert info["title"] == "t"
        assert info["base_ref"]   == "B0"
        assert info["source_ref"] == "S0"

    def test_get_pr_info_doesnt_overwrite_explicit_refs(self):
        fake = bf.FakeBitbucket({
            "pr_url": "x", "base_sha": "B0", "source_sha": "S0",
            "metadata": {"base_ref": "explicit-base", "source_ref": "explicit-src"},
        })
        info = fake.get_pr_info("x")
        assert info["base_ref"]   == "explicit-base"
        assert info["source_ref"] == "explicit-src"

    def test_get_pr_comments_normalises_nested_author_shape(self):
        fake = bf.FakeBitbucket({
            "comments": [{
                "id": 1, "text": "hi",
                "author": {"name": "Alice", "slug": "alice"},
                "anchor": {"path": "src/Foo.java", "line": 10},
            }],
        })
        cs = fake.get_pr_comments("x")
        assert len(cs) == 1
        c = cs[0]
        assert c["author"]      == "Alice"
        assert c["author_slug"] == "alice"
        assert c["file"]        == "src/Foo.java"
        assert c["line"]        == 10
        assert c["anchored"]    is True

    def test_get_pr_comments_normalises_flat_author_shape(self):
        fake = bf.FakeBitbucket({
            "comments": [{
                "id": 2, "text": "general note",
                "author": "bob", "author_slug": "bob-slug",
            }],
        })
        c = fake.get_pr_comments("x")[0]
        assert c["author"]      == "bob"
        assert c["author_slug"] == "bob-slug"
        # No anchor block → no file → not anchored.
        assert c["file"]     == ""
        assert c["anchored"] is False


# ─── comment thread rendering ─────────────────────────────────────────────


class TestCommentThread:
    """Tree shape:
         #1 (root, by alice)
           └─ #2 (reply, by bot)
                └─ #3 (reply, by carol)
         #10 (separate root)
    """

    def _build(self) -> bf.FakeBitbucket:
        return bf.FakeBitbucket({
            "comments": [
                {"id": 1,  "parent_id": 0, "text": "root",  "author": {"slug": "alice"}, "anchor": {"path": "Foo.java", "line": 1}},
                {"id": 2,  "parent_id": 1, "text": "mid",   "author": {"slug": "bot"}},
                {"id": 3,  "parent_id": 2, "text": "leaf",  "author": {"slug": "carol"}},
                {"id": 10, "parent_id": 0, "text": "other", "author": {"slug": "alice"}},
            ],
            "self_user": "bot",
        })

    def test_from_leaf_walks_up_to_root(self):
        rendered = self._build().get_comment_thread("x", comment_id=3)
        # All three nodes from chain 1→2→3 must appear.
        assert "#1 alice"   in rendered
        assert "#2 bot"     in rendered
        assert "#3 carol"   in rendered
        # The unrelated root #10 must NOT bleed in.
        assert "#10"        not in rendered

    def test_from_midpoint_walks_up(self):
        rendered = self._build().get_comment_thread("x", comment_id=2)
        assert "#1 alice" in rendered
        assert "#3 carol" in rendered  # children rendered too

    def test_self_user_gets_self_tag(self):
        rendered = self._build().get_comment_thread("x", comment_id=2)
        assert "#2 bot[SELF]" in rendered
        # Non-self speakers do NOT get the tag.
        assert "#1 alice[SELF]" not in rendered

    def test_indentation_grows_with_depth(self):
        rendered = self._build().get_comment_thread("x", comment_id=1)
        lines = rendered.splitlines()
        # Each level adds two spaces — depth 0/1/2 expected for the chain.
        assert lines[0].startswith("#1")
        assert lines[1].startswith("  #2")
        assert lines[2].startswith("    #3")

    def test_anchor_shown_only_when_present(self):
        rendered = self._build().get_comment_thread("x", comment_id=1)
        assert "Foo.java:1" in rendered     # root has anchor
        assert "(:" not in rendered          # no half-rendered anchors

    def test_unknown_comment_id_returns_empty(self):
        assert self._build().get_comment_thread("x", comment_id=999) == ""

    def test_no_comments_returns_empty(self):
        assert bf.FakeBitbucket({"comments": []}).get_comment_thread("x", 1) == ""

    def test_cyclic_parent_chain_does_not_hang(self):
        """Defensive — a malformed fixture with a cycle in parent_id
        must not infinite-loop the walker."""
        fake = bf.FakeBitbucket({"comments": [
            {"id": 1, "parent_id": 2, "text": "a", "author": "x"},
            {"id": 2, "parent_id": 1, "text": "b", "author": "y"},
        ]})
        # Should return *something* (not necessarily useful) but must terminate.
        _ = fake.get_comment_thread("x", comment_id=1)


# ─── write side (FakeBitbucket class) ─────────────────────────────────────


class TestWriteSide:
    def test_post_pr_comment_records_and_returns_new_id(self):
        fake = bf.FakeBitbucket({})
        nid = fake.post_pr_comment("x", text="hi", file="A.java",
                                   line=5, severity="major")
        assert isinstance(nid, int) and nid >= 10_000
        rec = fake.sink_records[-1]
        assert rec["kind"]     == "post_comment"
        assert rec["new_id"]   == nid
        assert rec["text"]     == "hi"
        assert rec["file"]     == "A.java"
        assert rec["line"]     == 5
        assert rec["severity"] == "major"

    def test_post_general_pr_comment_records(self):
        fake = bf.FakeBitbucket({})
        nid = fake.post_general_pr_comment("x", "Overall LGTM")
        assert nid >= 10_000
        rec = fake.sink_records[-1]
        assert rec["kind"] == "post_general"
        assert rec["text"] == "Overall LGTM"

    def test_reply_to_pr_comment_records(self):
        fake = bf.FakeBitbucket({})
        fake.reply_to_pr_comment("x", comment_id=42, text="agree")
        rec = fake.sink_records[-1]
        assert rec["kind"]      == "reply"
        assert rec["parent_id"] == 42
        assert rec["text"]      == "agree"

    def test_resolve_pr_comment_records(self):
        fake = bf.FakeBitbucket({})
        fake.resolve_pr_comment("x", comment_id=7)
        assert fake.sink_records[-1] == {"kind": "resolve", "comment_id": 7}

    def test_set_review_status_records(self):
        fake = bf.FakeBitbucket({})
        fake.set_review_status("x", user_slug="bot", status="NEEDS_WORK")
        assert fake.sink_records[-1] == {
            "kind": "set_status", "user_slug": "bot", "status": "NEEDS_WORK"
        }

    def test_auto_id_seeds_above_highest_existing_comment(self):
        """`_AUTO_ID` previously started at 10_000 unconditionally;
        with the rewrite it must seed above max(existing) to prevent
        collisions when fixture comments already use ids in that range."""
        fake = bf.FakeBitbucket({"comments": [{"id": 50_000, "text": "preseed"}]})
        nid = fake.post_pr_comment("x", text="post-existing")
        assert nid > 50_000

    def test_auto_id_monotonic_across_writes(self):
        fake = bf.FakeBitbucket({})
        ids = [fake.post_pr_comment("x", text=f"#{i}") for i in range(5)]
        assert ids == sorted(ids)
        assert len(set(ids)) == 5

    def test_post_review_comments_uses_body_then_text_fallback(self):
        """post_review_comments accepts either domain objects (with .body)
        or simpler dict-like things (with .text). Decorate fn applies."""
        fake = bf.FakeBitbucket({})

        class _C:
            def __init__(self, body=None, text=None, file="", line=0, severity=""):
                self.body, self.text = body, text
                self.file, self.line, self.severity = file, line, severity

        posted = fake.post_review_comments(
            "x",
            [_C(body="primary", file="A.java", line=1, severity="major"),
             _C(text="fallback", file="B.java", line=2, severity="minor")],
            decorate=lambda t: f"[bot] {t}",
        )
        assert posted == 2
        kinds = [r["kind"] for r in fake.sink_records]
        assert kinds == ["review_comment", "review_comment"]
        # decorate runs once per comment; both texts got the prefix.
        texts = [r["text"] for r in fake.sink_records]
        assert texts == ["[bot] primary", "[bot] fallback"]


class TestWriteSideFileSink:
    def test_file_sink_appends_jsonl(self, tmp_path):
        sink_file = tmp_path / "sink.jsonl"
        fake = bf.FakeBitbucket({}, sink_path=str(sink_file))
        fake.post_pr_comment("x", text="a")
        fake.set_review_status("x", user_slug="bot", status="NEEDS_WORK")
        # File sink runs IN ADDITION to in-memory sink_records — both
        # populated so a test can pick whichever is easier to assert on.
        assert len(fake.sink_records) == 2
        lines = sink_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        recs = [json.loads(l) for l in lines]
        assert recs[0]["kind"] == "post_comment"
        assert recs[1]["kind"] == "set_status"

    def test_file_sink_failure_does_not_crash_agent(self, tmp_path):
        """If the sink file becomes unwritable mid-run, the agent
        should not crash — the write is swallowed defensively.
        Pointing at a non-existent directory path triggers the open()
        failure path."""
        bad = tmp_path / "no-such-dir" / "sink.jsonl"
        fake = bf.FakeBitbucket({}, sink_path=str(bad))
        # Must NOT raise.
        fake.post_pr_comment("x", text="a")
        # In-memory sink still works.
        assert fake.sink_records[-1]["text"] == "a"


# ─── module-level routing (legacy subprocess path) ────────────────────────


class TestModuleRouting:
    def test_install_replaces_singleton(self, two_commit_repo):
        repo, base, source = two_commit_repo
        fake = bf.FakeBitbucket({
            "pr_url": "fake://x", "repo_path": str(repo),
            "base_sha": base, "source_sha": source,
            "metadata": {"title": "installed"},
        })
        bf.install(fake)
        # Module-level call must route through the installed instance.
        info = bf.get_pr_info("fake://x")
        assert info["title"] == "installed"
        # And writes hit the installed instance's sink, not a new one.
        bf.post_pr_comment("fake://x", text="from-module")
        assert fake.sink_records[-1]["text"] == "from-module"

    def test_reset_forgets_installed_instance(self):
        fake = bf.FakeBitbucket({"metadata": {"title": "A"}})
        bf.install(fake)
        assert bf.get_pr_info("x")["title"] == "A"
        bf.reset()
        # Next call lazily reinitialises — no env var set, so empty shell.
        info = bf.get_pr_info("x")
        assert info.get("title", "") == ""

    def test_env_init_reads_payload_file(self, tmp_path, monkeypatch):
        """When no instance is installed but DIFFGRAPH_FAKE_PR_FILE is
        set, the singleton must lazily read the payload from disk on
        first call. This is the bench subprocess workflow."""
        payload = {
            "pr_url": "fake://env", "repo_path": "/x",
            "base_sha": "B", "source_sha": "S",
            "metadata": {"title": "from-env"},
            "comments": [], "self_user": "bot",
        }
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("DIFFGRAPH_FAKE_PR_FILE", str(p))
        # No install() — lazy init must pick up the env var.
        info = bf.get_pr_info("fake://env")
        assert info["title"] == "from-env"

    def test_env_init_with_sink_path_writes_jsonl(self, tmp_path, monkeypatch):
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps({}), encoding="utf-8")
        sink_path = tmp_path / "sink.jsonl"
        monkeypatch.setenv("DIFFGRAPH_FAKE_PR_FILE", str(payload_path))
        monkeypatch.setenv("DIFFGRAPH_FAKE_PR_SINK", str(sink_path))
        bf.post_pr_comment("fake://", text="hello")
        lines = sink_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "hello"

    def test_two_instances_have_independent_state(self):
        """The whole point of the class rewrite — two tests holding
        two fakes side-by-side don't cross-contaminate."""
        a = bf.FakeBitbucket({"comments": [{"id": 1, "text": "A", "author": "alice"}]})
        b = bf.FakeBitbucket({"comments": [{"id": 2, "text": "B", "author": "bob"}]})

        # Write to a, then b. Each only sees its own writes.
        a.post_pr_comment("x", text="from-a")
        b.post_pr_comment("x", text="from-b")
        assert [r["text"] for r in a.sink_records] == ["from-a"]
        assert [r["text"] for r in b.sink_records] == ["from-b"]

        # Reads are independent too — a's comments don't leak into b's view.
        assert a.get_pr_comments("x")[0]["author"] == "alice"
        assert b.get_pr_comments("x")[0]["author"] == "bob"

    def test_module_api_parity_with_class(self):
        """The module-level wrappers should be 1:1 with the class
        methods — calling either against the same instance gives the
        same result."""
        fake = bf.FakeBitbucket({})
        bf.install(fake)

        # Generate via module-level.
        nid_mod = bf.post_pr_comment("x", text="mod")
        # Generate via class.
        nid_cls = fake.post_pr_comment("x", text="cls")
        # Both went into the same sink, in order.
        assert [r["text"] for r in fake.sink_records] == ["mod", "cls"]
        # Auto-ids are monotonic across module/class entrypoints.
        assert nid_cls > nid_mod


# ─── installed-fake driving a real cli.py-shaped read flow ────────────────


class TestEndToEndFlow:
    """One more "integration" check — go through a complete
    PR-fetch + comment-thread + post-reply sequence the way cli.py
    would when it routes via _run_with_dispatcher: fetch_pr to
    materialise the diff, get_pr_comments to find the trigger,
    get_comment_thread for the bot's context, reply via post_pr_comment.
    Verifies all four module-level entrypoints chain on one instance."""

    def test_full_review_flow(self, two_commit_repo):
        repo, base, source = two_commit_repo
        fake = bf.FakeBitbucket({
            "pr_url":     "fake://x/Y/repos/Z/pull-requests/1",
            "repo_path":  str(repo),
            "base_sha":   base,
            "source_sha": source,
            "metadata":   {"title": "review me"},
            "comments": [
                {"id": 1, "parent_id": 0, "text": "/review",
                 "author": {"slug": "human"}},
            ],
            "self_user": "diffgraph-bot",
        })
        bf.install(fake)

        # 1. fetch the PR — real git diff against the tmp repo.
        diff, cwd, _cleanup, meta = bf.fetch_pr("fake://x/Y/repos/Z/pull-requests/1")
        assert "README.md" in diff
        assert meta["title"] == "review me"

        # 2. read the trigger comment.
        cs = bf.get_pr_comments("fake://x/Y/repos/Z/pull-requests/1")
        assert cs[0]["text"] == "/review"

        # 3. render the thread for context.
        rendered = bf.get_comment_thread("fake://x/Y/repos/Z/pull-requests/1",
                                         comment_id=1)
        assert "#1 human" in rendered

        # 4. post a reply + set verdict.
        bf.post_pr_comment("fake://x/Y/repos/Z/pull-requests/1",
                           text="LGTM", file="README.md", line=2,
                           severity="minor", parent_id=1)
        bf.set_review_status("fake://x/Y/repos/Z/pull-requests/1",
                             user_slug="diffgraph-bot", status="APPROVED")

        # All four actions visible on the single shared sink, in order.
        kinds = [r["kind"] for r in fake.sink_records]
        assert kinds == ["post_comment", "set_status"]
