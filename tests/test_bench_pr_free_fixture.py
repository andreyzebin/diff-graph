"""Bench: PR-free unit fixtures (no `repo:` block) load successfully.

The `repo:` field used to be mandatory in `run_unit::load_fixture` —
every fixture had to declare a git checkout, even when the agent
under test never touched diff_* tools. Abstract / skill-only
scenarios (SKILL-001-*) now skip the block entirely; the runner
no-ops the clone + fake_bitbucket plumbing.

Pinned here:
- Yaml without `repo:` parses, `fixture.repo_source is None`.
- The shipped SKILL-001-prefer-delegation-{with,without} scenarios
  load without raising.
- A yaml with `repo:` still loads (back-compat).
- An invalid repo.source (path with no .git) still raises (we kept
  the safety check inside the optional branch).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from benchmarks.runner.run_unit import load_fixture


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fixture.yaml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


# ── No `repo:` block ──────────────────────────────────────────────


class TestPRFreeFixtures:

    def test_no_repo_block_loads(self, tmp_path):
        fixture_path = _write(tmp_path, """
            id: TEST-001-skill
            agent: boss
            user_message_from: diffgraph:diffgraph/test_prompts/skills/boss.user.md
            agent_data:
              task_input: "21"
        """)
        fx = load_fixture(fixture_path)
        assert fx.repo_source is None
        assert fx.agent == "boss"
        assert fx.agent_data["task_input"] == "21"

    def test_shipped_skill_scenarios_load(self):
        """Both SKILL-001 variants ship in the repo — they must
        load without a repo block."""
        root = Path(__file__).resolve().parent.parent / "benchmarks" / "scenarios" / "unit" / "skills"
        for name in (
            "SKILL-001-prefer-delegation-with.yaml",
            "SKILL-001-prefer-delegation-without.yaml",
        ):
            fp = root / name
            assert fp.exists(), f"missing scenario file: {fp}"
            fx = load_fixture(fp)
            assert fx.repo_source is None, f"{name}: expected PR-free"
            assert fx.agent == "boss"


# ── Back-compat: yaml WITH `repo:` still works ────────────────────


class TestRepoBlockStillWorks:

    def test_invalid_repo_source_raises(self, tmp_path):
        """The safety check (path must be a git checkout) lives
        inside the optional branch — when `repo:` IS declared but
        the path isn't a git repo, we still raise. Surfaces typos
        at fixture-load time."""
        fixture_path = _write(tmp_path, f"""
            id: TEST-002
            agent: reviewer
            repo:
              source: {tmp_path / "not-a-repo"}
              source_branch: main
        """)
        # Create the directory but not as a git repo.
        (tmp_path / "not-a-repo").mkdir()
        with pytest.raises(FileNotFoundError, match="not a git checkout"):
            load_fixture(fixture_path)
