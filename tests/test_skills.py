"""Skills — composable bundles of tools + rationale that user
prompts opt into via the `skills:` frontmatter field.

Pinned here:
- Skill files in `orchestra/skills/*.md` load via the same
  frontmatter parser as agent prompts (consistent UX).
- mount_skills mutates _fm_meta in place (extends tools_add and
  extra_tools) so existing register loops + _build_tool_names
  pick up the contributions without new dispatch paths.
- Aggregated body is the {skills} placeholder content with per-
  skill headers so multi-skill prompts have visual separation.
- Missing skill / wrong shape → ValueError at agent-init time
  with the catalog of available skills surfaced for typos.
- The shipped `delegation_depth_as_upgrade` skill loads and
  resolves end-to-end through Agent.__init__.
"""
from __future__ import annotations

import textwrap

import pytest

from orchestra.skills import (
    Skill,
    list_skills,
    load_skill,
    mount_skills,
)


# ── Catalog discovery ──────────────────────────────────────────────


class TestCatalog:

    def test_list_includes_shipped_skill(self):
        names = list_skills()
        assert "delegation_depth_as_upgrade" in names


# ── load_skill ─────────────────────────────────────────────────────


class TestLoadSkill:

    def test_loads_shipped_skill(self):
        skill = load_skill("delegation_depth_as_upgrade")
        assert isinstance(skill, Skill)
        assert skill.name == "delegation_depth_as_upgrade"
        # The shipped skill bundles agent_spawn + agent_list.
        assert "agent_spawn" in skill.tools
        assert "agent_list" in skill.tools
        # And a rationale body that lands as the {skills}
        # placeholder content.
        assert "Delegation" in skill.body
        assert "delegate" in skill.body.lower()

    def test_missing_skill_lists_available(self):
        with pytest.raises(ValueError) as exc:
            load_skill("does_not_exist_yet")
        msg = str(exc.value)
        assert "does_not_exist_yet" in msg
        # Catalog leaks into the error so the author can spot a typo.
        assert "delegation_depth_as_upgrade" in msg

    def test_wrong_tools_shape_raises(self, tmp_path, monkeypatch):
        # Build a fake skill file with `tools` as a string (wrong).
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "bad.md").write_text(
            "---\ntools: not_a_list\n---\nbody\n", encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        with pytest.raises(ValueError, match="must be a list"):
            skills_mod.load_skill("bad")


# ── mount_skills ───────────────────────────────────────────────────


class TestMountSkills:

    def test_no_skills_returns_empty_string(self):
        fm = {}
        out = mount_skills([], fm_meta=fm)
        assert out == ""
        # _fm_meta is untouched — tools_add stays as the agent
        # itself declared (or absent).
        assert "tools_add" not in fm

    def test_mounts_tools_into_tools_add(self):
        fm = {"tools_add": ["text_answer"]}
        mount_skills(["delegation_depth_as_upgrade"], fm_meta=fm)
        # Skill's tools landed alongside the prompt's existing
        # tools_add — both visible to _build_tool_names.
        assert "text_answer" in fm["tools_add"]
        assert "agent_spawn" in fm["tools_add"]
        assert "agent_list" in fm["tools_add"]

    def test_body_has_per_skill_header(self):
        body = mount_skills(["delegation_depth_as_upgrade"], fm_meta={})
        assert body.startswith("## Skill: delegation_depth_as_upgrade")

    def test_multiple_skills_concatenate(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "skill_a.md").write_text(
            "---\ntools: [tool_a]\n---\nA body.\n", encoding="utf-8",
        )
        (fake_dir / "skill_b.md").write_text(
            "---\ntools: [tool_b]\n---\nB body.\n", encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        fm: dict = {}
        body = skills_mod.mount_skills(
            ["skill_a", "skill_b"], fm_meta=fm,
        )
        # Order preserved, headers per-skill, both bodies present.
        assert body.index("## Skill: skill_a") < body.index("## Skill: skill_b")
        assert "A body." in body and "B body." in body
        # Tools from both merged.
        assert fm["tools_add"] == ["tool_a", "tool_b"]

    def test_extra_tools_merge(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "with_capture.md").write_text(
            textwrap.dedent("""
                ---
                tools: []
                extra_tools:
                  - name: probe
                    description: "test capture"
                    parameters:
                      type: object
                      properties: {}
                ---
                Body.
            """).lstrip(), encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        fm: dict = {"extra_tools": [
            {"name": "user_own", "description": "u", "parameters": {}},
        ]}
        skills_mod.mount_skills(["with_capture"], fm_meta=fm)
        names = [t["name"] for t in fm["extra_tools"]]
        assert names == ["user_own", "probe"]


# ── End-to-end via Agent.__init__ ─────────────────────────────────


class TestAgentIntegration:
    """Agent.__init__ mounts skills from user_message_override's
    frontmatter. tools surface ends up as the union of prompt's
    own tools_add and skill's tools; {skills} placeholder
    resolves in the user message."""

    def _agent(self, override: str):
        from orchestra.agent import Agent
        from orchestra.tools.registry import ToolRegistry
        from orchestra.types import AgentConfig
        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["reflect", "done"],
        )
        return Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )

    def test_skill_tools_merge_into_effective_surface(self):
        override = (
            "---\n"
            "skills: [delegation_depth_as_upgrade]\n"
            "---\n"
            "body\n"
        )
        a = self._agent(override)
        # The skill's tools landed in tools_add via mount_skills.
        tools_add = a._fm_meta.get("tools_add", [])
        assert "agent_spawn" in tools_add
        assert "agent_list" in tools_add

    def test_placeholder_resolves_to_skill_body(self):
        override = (
            "---\n"
            "skills: [delegation_depth_as_upgrade]\n"
            "---\n"
            "Task body.\n\n{{ skills }}\n\nSynthesis line.\n"
        )
        a = self._agent(override)
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        # Placeholder literal gone, skill content + header inlined.
        assert "{{ skills }}" not in user["content"]
        assert "## Skill: delegation_depth_as_upgrade" in user["content"]
        assert "Delegation" in user["content"]

    def test_no_skills_no_placeholder_unchanged(self):
        a = self._agent("Plain body, no skills, no placeholder.")
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        assert user["content"].strip() == "Plain body, no skills, no placeholder."

    def test_skill_with_placeholder_but_no_declaration_renders_empty(self):
        """If a prompt references `{{ skills }}` but declares no
        `skills:` block, the placeholder collapses to empty —
        no crash, no literal leak. Defensive: prompts can carry
        the placeholder even when no skills are mounted for a
        particular scenario."""
        override = (
            "---\n"
            "tools_add: [done]\n"
            "---\n"
            "Body with {{ skills }} placeholder.\n"
        )
        a = self._agent(override)
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        assert "{{ skills }}" not in user["content"]
        assert "Body with" in user["content"]

    def test_invalid_skills_type_raises(self):
        override = (
            "---\n"
            "skills: not_a_list\n"
            "---\n"
            "body\n"
        )
        with pytest.raises(ValueError, match="skills must be a list"):
            self._agent(override)
