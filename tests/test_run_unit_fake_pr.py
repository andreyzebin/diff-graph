"""
Every unit fixture — including investigator scenarios without an
explicit `pr_state` block — must get a fake-PR plumbed in via
DIFFGRAPH_FAKE_PR_FILE + `--pr-url=fake://…`.

Background: diff-graph's `cli.py run --agent=…` path always goes
through `_run_with_dispatcher`, which builds a `_Ctx(_initialized=
False, _init_fn=_lazy_init)`. The first call to ANY domain tool
(`diff_list_files`, `diff_search`, …) triggers `_lazy_init`, which
calls `fetch_pr(pr_url)` → `parse_pr_url(pr_url)`. With an empty
pr_url that raises "Cannot find 'projects' segment in PR URL: ",
the agent runner wraps it as `"error: …"`, and every subsequent
tool call returns the same error. Investigators on
investigator-tier unit fixtures (no `pr_state` block) end up
running blind.

Fix is on the bench side: always synthesize the fake-PR payload
(empty comments + bare metadata when the fixture doesn't provide
one) so the bitbucket_fake provider can answer fetch_pr/get_pr_info
just like it does for reviewer fixtures.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from runner import run_unit as ru


@pytest.fixture
def investigator_fixture():
    """A real investigator unit fixture from the repo — by design
    has no `pr_state` block (focus-only, no PR plumbing)."""
    p = (Path(__file__).resolve().parents[1] / "scenarios" / "unit"
         / "investigator" / "INV-U-001-cancel-npe.yaml")
    return ru.load_fixture(p)


@pytest.fixture
def reviewer_fixture():
    """A reviewer unit fixture WITH a `pr_state` block — must keep
    working exactly as before."""
    p = (Path(__file__).resolve().parents[1] / "scenarios" / "unit"
         / "reviewer" / "REV-U-001-store-credit-concerns.yaml")
    return ru.load_fixture(p)


class _Captured(Exception):
    """Sentinel — fires from the mocked subprocess.run AFTER cmd/env
    have been captured, so we don't need to mock the post-subprocess
    cleanup (which would otherwise `shutil.rmtree(tmp_repo.parent)`,
    i.e. `/tmp`)."""


def _patched_run_unit(fixture):
    """Drive `run_unit_fixture` up to its subprocess invocation,
    capture cmd/env, then bail. Returns the captured dict."""
    captured: dict = {}

    def _capture_run(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw.get("env") or {})
        # Snapshot the fake-PR payload while the file still exists —
        # run_unit_fixture's `finally:` block will unlink it after we
        # raise out of subprocess.run.
        fp = captured["env"].get("DIFFGRAPH_FAKE_PR_FILE")
        if fp and Path(fp).exists():
            captured["payload"] = json.loads(Path(fp).read_text())
        raise _Captured()

    fake_path = Path(__file__).resolve().parents[1] / "scenarios" / "unit"
    with patch.object(ru, "load_fixture", return_value=fixture), \
         patch.object(ru, "_clone_local", return_value=Path("/tmp/fake-repo")), \
         patch.object(ru, "_checkout_refs", return_value=("BASE_SHA", "SRC_SHA")), \
         patch.object(subprocess, "run", side_effect=_capture_run):
        try:
            ru.run_unit_fixture(fake_path / "dummy.yaml", provider="deepseek",
                                timeout=60)
        except _Captured:
            pass
    assert "cmd" in captured, "subprocess.run was never reached"
    return captured


def _kv(env: dict, key: str) -> str:
    """Read an env var with a friendly assertion message."""
    assert key in env, f"missing env: {key}\nhave: {sorted(env)}"
    return env[key]


def _flag(cmd: list[str], flag: str) -> str:
    """Get the value following a `--flag` argument in cmd."""
    for i, a in enumerate(cmd):
        if a == flag and i + 1 < len(cmd):
            return cmd[i + 1]
    raise AssertionError(f"flag {flag!r} not in cmd: {cmd}")


class TestFakePrAlwaysPlumbed:
    def test_investigator_no_pr_state_gets_fake_pr(self, investigator_fixture):
        """The bug we're fixing — without this, investigator unit
        fixtures run without --pr-url and the lazy ctx blows up on
        first tool call."""
        # Sanity-check the fixture really has no pr_state — otherwise
        # this test wouldn't exercise the regression.
        assert not investigator_fixture.pr_state, \
            "investigator fixture unexpectedly has pr_state — pick another"

        cap = _patched_run_unit(investigator_fixture)
        cmd = cap["cmd"]
        env = cap["env"]

        # Fake-PR env must be present. The file gets cleaned up by
        # run_unit_fixture's `finally:`, but we snapshotted the payload
        # during _capture_run while the file was still on disk.
        _kv(env, "DIFFGRAPH_FAKE_PR_FILE")
        _kv(env, "DIFFGRAPH_FAKE_PR_SINK")

        # --pr-url must be passed; must be a fake:// URL so cli.py
        # routes through bitbucket_fake (not a real HTTP fetch).
        pr_url = _flag(cmd, "--pr-url")
        assert pr_url.startswith("fake://"), \
            f"expected fake:// PR URL, got: {pr_url}"
        assert investigator_fixture.fixture_id in pr_url, \
            f"PR URL should embed fixture id ({investigator_fixture.fixture_id}): {pr_url}"

        payload = cap["payload"]
        assert payload["pr_url"] == pr_url
        assert payload["base_sha"] == "BASE_SHA"
        assert payload["source_sha"] == "SRC_SHA"
        # No pr_state ⇒ empty comments + empty metadata + default bot user.
        assert payload["comments"] == []
        assert payload["metadata"] == {}
        assert payload["self_user"] == "diffgraph-bot"

    def test_investigator_no_message_or_comment_id(self, investigator_fixture):
        """Investigator fixtures don't carry triggers — even though
        we now wire --pr-url for them, we must NOT inject --message /
        --comment-id (those are reviewer/dispatcher concepts)."""
        cap = _patched_run_unit(investigator_fixture)
        cmd = cap["cmd"]
        assert "--message" not in cmd, f"unexpected --message: {cmd}"
        assert "--comment-id" not in cmd, f"unexpected --comment-id: {cmd}"

    def test_reviewer_with_pr_state_still_passes_trigger(self, reviewer_fixture):
        """Reviewer fixtures keep getting --pr-url AND the trigger
        plumbing (--message / --comment-id when present in the
        fixture's `trigger` block)."""
        assert reviewer_fixture.pr_state, \
            "reviewer fixture unexpectedly has no pr_state"

        cap = _patched_run_unit(reviewer_fixture)
        cmd = cap["cmd"]
        env = cap["env"]

        _kv(env, "DIFFGRAPH_FAKE_PR_FILE")
        pr_url = _flag(cmd, "--pr-url")
        assert pr_url.startswith("fake://"), pr_url

        # The store-credit-concerns reviewer fixture has a trigger.text;
        # confirm it gets through to --message.
        trig = reviewer_fixture.trigger or {}
        if trig.get("text"):
            assert _flag(cmd, "--message") == str(trig["text"])


class TestFakePayloadShape:
    def test_payload_defensive_against_empty_pr_state(self, investigator_fixture):
        """`_build_fake_pr_payload` is the bottleneck — it must
        produce a complete payload from a fixture whose `pr_state`
        is empty/None."""
        assert not investigator_fixture.pr_state
        payload = ru._build_fake_pr_payload(
            investigator_fixture, Path("/tmp/r"), "B", "S"
        )
        assert payload["pr_url"].startswith("fake://")
        assert payload["repo_path"] == "/tmp/r"
        assert payload["base_sha"] == "B"
        assert payload["source_sha"] == "S"
        assert payload["comments"] == []
        assert payload["metadata"] == {}
        assert payload["self_user"]  # has a default

    def test_payload_threads_pr_state_metadata_when_present(self, reviewer_fixture):
        """When pr_state IS present, metadata + comments + self_user
        flow through from the fixture (regression guard for the
        reviewer path)."""
        assert reviewer_fixture.pr_state
        payload = ru._build_fake_pr_payload(
            reviewer_fixture, Path("/tmp/r"), "B", "S"
        )
        pr_state_md = (reviewer_fixture.pr_state.get("metadata") or {})
        # Either metadata is forwarded (if present in fixture) or
        # default-empty (if fixture didn't set any). Test that
        # whatever the fixture has, the payload mirrors.
        assert payload["metadata"] == pr_state_md
        # comments list — same identity check
        fixture_comments = reviewer_fixture.pr_state.get("comments") or []
        assert payload["comments"] == fixture_comments
