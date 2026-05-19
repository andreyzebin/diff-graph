"""Skills — composable bundles of tools + rationale that user
prompts opt into via the `skills:` frontmatter field.

Pinned here:
- Skill files in `orchestra/skills/*.md` load via the same
  frontmatter parser as agent prompts (consistent UX).
- mount_skills mutates _fm_meta in place (extends tools and
  extra_tools) so existing register loops + _build_tool_names
  pick up the contributions without new dispatch paths.
- Aggregated body is the {skills} placeholder content with per-
  skill headers so multi-skill prompts have visual separation.
- Missing skill / wrong shape → ValueError at agent-init time
  with the catalog of available skills surfaced for typos.
- The shipped `prefer_delegation` skill loads and
  resolves end-to-end through Agent.__init__.
"""
from __future__ import annotations

import textwrap

import pytest

from orchestra.skills import (
    Skill,
    list_skill_listings,
    list_skills,
    load_skill,
    mount_skills,
)


# ── Catalog discovery ──────────────────────────────────────────────


class TestCatalog:

    def test_list_includes_shipped_skill(self):
        names = list_skills()
        assert "prefer_delegation" in names

    def test_listings_carry_description_and_tools(self):
        """`list_skill_listings()` is the agent-facing catalog
        (analog of agent_list's to_listing). One entry per skill,
        each with name/description/tools/extra_tools — enough for
        an agent (or prompt author) to pick a skill without
        opening the file."""
        listings = list_skill_listings()
        # Index by name for a stable lookup regardless of order.
        by_name = {e["name"]: e for e in listings}
        assert "prefer_delegation" in by_name
        entry = by_name["prefer_delegation"]
        # Description present and non-empty — strategy summary.
        # Loose keyword check: the prefer_delegation skill's
        # description should mention the action verb (route /
        # delegate / spawn) so a catalog reader can infer what
        # it does.
        assert entry["description"]
        desc = entry["description"].lower()
        assert any(kw in desc for kw in ("delegate", "spawn", "route"))
        # Tools listed verbatim from the skill file.
        assert "agent_spawn" in entry["tools"]
        assert "agent_list" in entry["tools"]
        # No extra_tools on this skill (capture-style schemas
        # would land here if the skill declared any).
        assert entry["extra_tools"] == []


# ── load_skill ─────────────────────────────────────────────────────


class TestLoadSkill:

    def test_loads_shipped_skill(self):
        skill = load_skill("prefer_delegation")
        assert isinstance(skill, Skill)
        assert skill.name == "prefer_delegation"
        # The shipped skill bundles agent_spawn + agent_list.
        assert "agent_spawn" in skill.tools
        assert "agent_list" in skill.tools
        # And a rationale body that lands as the {skills}
        # placeholder content.
        assert "Delegation" in skill.body
        assert "delegate" in skill.body.lower()
        # Description is the short strategy summary visible in
        # `list_skill_listings()` — required for the catalog UX
        # to make sense.
        assert skill.description
        desc = skill.description.lower()
        assert any(kw in desc for kw in ("delegate", "spawn", "route"))

    def test_missing_skill_lists_available(self):
        with pytest.raises(ValueError) as exc:
            load_skill("does_not_exist_yet")
        msg = str(exc.value)
        assert "does_not_exist_yet" in msg
        # Catalog leaks into the error so the author can spot a typo.
        assert "prefer_delegation" in msg

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
        # _fm_meta is untouched — tools stays as the agent
        # itself declared (or absent).
        assert "tools" not in fm

    def test_mounts_tools_into_tools(self):
        fm = {"tools": ["text_answer"]}
        mount_skills(["prefer_delegation"], fm_meta=fm)
        # Skill's tools landed alongside the prompt's existing
        # tools — both visible to _build_tool_names.
        assert "text_answer" in fm["tools"]
        assert "agent_spawn" in fm["tools"]
        assert "agent_list" in fm["tools"]

    def test_body_has_per_skill_header(self):
        body = mount_skills(["prefer_delegation"], fm_meta={})
        assert body.startswith("## Skill: prefer_delegation")

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
        assert fm["tools"] == ["tool_a", "tool_b"]

    def test_reflect_supplied_when_prompt_unset(
        self, tmp_path, monkeypatch,
    ):
        """A skill can declare `reflect:` to say "I need this
        feature on". mount_skills writes each skill reflect key
        into fm_meta only if the prompt hasn't already set the
        same key — prompt's explicit choice always wins."""
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "needs_state.md").write_text(
            "---\ntools: []\nreflect:\n  with_state: true\n---\nbody\n",
            encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        # Empty fm_meta → skill's value lands.
        fm: dict = {}
        skills_mod.mount_skills(["needs_state"], fm_meta=fm)
        assert fm["reflect"] == {"with_state": True}

    def test_prompt_overrides_skill_reflect(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "needs_state.md").write_text(
            "---\ntools: []\nreflect:\n  with_state: true\n---\nbody\n",
            encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        # Prompt already set the same key → skill respects it.
        fm: dict = {"reflect": {"with_state": False}}
        skills_mod.mount_skills(["needs_state"], fm_meta=fm)
        assert fm["reflect"]["with_state"] is False

    def test_first_skill_wins_over_later_skill_for_same_key(
        self, tmp_path, monkeypatch,
    ):
        """Multiple skills in the `skills: [a, b]` list: first
        skill's reflect key wins per-key over later skills'.
        Mirrors the prompt-wins rule one level down."""
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "first.md").write_text(
            "---\ntools: []\nreflect: { with_state: true }\n---\nbody\n",
            encoding="utf-8",
        )
        (fake_dir / "second.md").write_text(
            "---\ntools: []\nreflect: { with_state: false }\n---\nbody\n",
            encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)
        fm: dict = {}
        skills_mod.mount_skills(["first", "second"], fm_meta=fm)
        # First wins on key `with_state`.
        assert fm["reflect"]["with_state"] is True

    def test_shipped_prefer_delegation_supplies_reflect(self):
        """prefer_delegation declares `reflect: { with_state:
        true }` — the live snapshot is part of the skill's
        contract. Verified end-to-end: a prompt mounting the
        skill ends up with the key set in fm_meta without
        having to redeclare it itself."""
        fm: dict = {}
        mount_skills(["prefer_delegation"], fm_meta=fm)
        assert fm["reflect"]["with_state"] is True

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
    own tools and skill's tools; {skills} placeholder
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
            "skills: [prefer_delegation]\n"
            "---\n"
            "body\n"
        )
        a = self._agent(override)
        # The skill's tools landed in tools via mount_skills.
        tools = a._fm_meta.get("tools", [])
        assert "agent_spawn" in tools
        assert "agent_list" in tools

    def test_skills_inject_as_separate_system_message(self):
        """Framework injects the rendered skill body as a SEPARATE
        system-role message between the agent's own system prompt
        and the conversation/user task. No per-prompt placeholder
        needed — the user.md body stays clean."""
        override = (
            "---\n"
            "skills: [prefer_delegation]\n"
            "---\n"
            "Task body.\n"
        )
        a = self._agent(override)
        msgs = a._build_messages()
        system_msgs = [m for m in msgs if m.get("role") == "system"]
        # First system message is the agent's own; the injected
        # skill block follows as a second system message.
        assert len(system_msgs) == 2
        skill_msg = system_msgs[1]
        assert "## Skill: prefer_delegation" in skill_msg["content"]
        assert "Delegation" in skill_msg["content"]
        # User message stays clean — no skill content bleeding in.
        user = next(m for m in msgs if m.get("role") == "user")
        assert "## Skill:" not in user["content"]
        assert user["content"].strip() == "Task body."

    def test_no_skills_no_extra_system_message(self):
        """Agent with no `skills:` declared gets the same single-
        system-message shape as before (no skill injection)."""
        a = self._agent("Plain body, no skills.")
        msgs = a._build_messages()
        system_msgs = [m for m in msgs if m.get("role") == "system"]
        assert len(system_msgs) == 1
        user = next(m for m in msgs if m.get("role") == "user")
        assert user["content"].strip() == "Plain body, no skills."

    def test_stale_placeholder_in_user_md_renders_empty(self):
        """Backward-compat: a user.md still carrying a stale
        `{{ skills }}` placeholder renders it as empty (the
        framework no longer feeds skill body to the template) —
        no double-render, no literal leak. New prompts should
        DROP the placeholder; the framework handles injection."""
        override = (
            "---\n"
            "tools: [done]\n"
            "---\n"
            "Body with {{ skills }} placeholder.\n"
        )
        a = self._agent(override)
        msgs = a._build_messages()
        user = next(m for m in msgs if m.get("role") == "user")
        assert "{{ skills }}" not in user["content"]
        assert "Body with" in user["content"]

    def test_skill_reflect_lands_on_config(self):
        """End-to-end: a prompt mounting `prefer_delegation`
        without its own reflect block ends up with
        `config.reflect["with_state"] == True`, because the
        skill supplies it during mount_skills (which runs
        before the per-area merge block in Agent.__init__)."""
        override = (
            "---\n"
            "skills: [prefer_delegation]\n"
            "---\n"
            "body\n"
        )
        a = self._agent(override)
        assert a.config.reflect.get("with_state") is True

    def test_skill_brings_reflect_creates_sgr_tracker(self, tmp_path, monkeypatch):
        """Regression: when reflect arrives via a skill (not via
        the agent's base `tools:` list), the agent's SGR tracker
        and the reflect-cadence pusher must still wire up. Before
        the fix, Agent.__init__ checked `reflect in config.tools`
        (base only) — missing skill-mounted reflect → sgr=None →
        cadence pusher silently neutered → models never get
        nudged to reflect on long chains.
        """
        fake_dir = tmp_path / "skills"
        fake_dir.mkdir()
        (fake_dir / "reflect.md").write_text(
            "---\ntools: [reflect]\nreflect: { interval: 5 }\n---\n"
            "Reflect skill body.\n", encoding="utf-8",
        )
        from orchestra import skills as skills_mod
        monkeypatch.setattr(skills_mod, "_SKILLS_DIR", fake_dir)

        from orchestra.agent import Agent
        from orchestra.tools.registry import ToolRegistry
        from orchestra.types import AgentConfig
        # Crucial: `reflect` is NOT in the base tools list. It
        # arrives ONLY via the skill mount below.
        cfg = AgentConfig(
            name="probe", system_prompt="sys", user_prompt="base",
            tools=["done"],
        )
        override = (
            "---\n"
            "skills: [reflect]\n"
            "---\n"
            "body\n"
        )
        a = Agent(
            config=cfg,
            tool_registry=ToolRegistry(),
            llm=None,
            model="probe-model",
            user_message_override=override,
        )
        # SGR tracker MUST exist — without it, reflect-cadence
        # pushers all no-op (gated by `if self.sgr`).
        assert a.sgr is not None, (
            "Agent.sgr is None despite skill mounting reflect — "
            "_init must check post-skill effective tools, not "
            "just config.tools."
        )
        # Cadence config flowed through too — skill's interval=5
        # landed on config.reflect via the per-area merge.
        assert a.config.reflect.get("interval") == 5
        # Reflect cadence pusher is in the budget tracker's chain
        # (sanity check that the wiring downstream of sgr fired).
        pusher_names = [type(h).__name__ for h in a.budget_tracker.handlers]
        assert "ReflectCadencePusher" in pusher_names
        assert "ReflectCadenceCounter" in pusher_names

    def test_invalid_skills_type_raises(self):
        override = (
            "---\n"
            "skills: not_a_list\n"
            "---\n"
            "body\n"
        )
        with pytest.raises(ValueError, match="skills must be a list"):
            self._agent(override)
