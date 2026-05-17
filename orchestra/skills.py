"""Skills — composable bundles of tools + rationale that user
prompts opt into via the `skills:` frontmatter field.

A skill bundles:
- `tools:` — names of tools the skill needs in the agent's
  effective tool surface (merged into the prompt's tools_add).
- `extra_tools:` — capture-style tool schemas (same shape as
  user-message frontmatter `extra_tools`).
- body — the prose explaining how to use the bundled tools (the
  rationale).

When a user prompt declares `skills: [name1, name2]`, the
framework mounts each at Agent.__init__: tools merge into the
effective surface, extra_tools register, and the bodies are
concatenated under a `{{ skills }}` placeholder for the user
prompt to render (via the Jinja template engine).

Skills live in `orchestra/skills/<name>.md` (the same plain-md
+ frontmatter shape used by agent prompts; we reuse
orchestra.prompts.frontmatter.parse). Discoverability: `ls
orchestra/skills/` shows the catalog.

V1 is static — skills declared in user-message frontmatter at
agent-init time. Future (TODO §17): dynamic attachment via an
`attach_skill(name)` tool call with a framework NUDGE telling the
agent "you activated skill X — here's its rationale + new tools".
The same mount_skills() entry point would run from the tool
handler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .prompts.frontmatter import parse as _fm_parse


log = logging.getLogger(__name__)


_SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass
class Skill:
    """In-memory representation of a loaded skill file."""
    name: str
    # One-line summary of what this skill changes about the
    # agent's strategy. Shown in skill catalogs (future
    # `agent_list_skills()` tool) so an agent or prompt author
    # can pick a skill without reading the full body. Empty
    # when the skill file doesn't declare one.
    description: str = ""
    tools: list[str] = field(default_factory=list)
    extra_tools: list[dict] = field(default_factory=list)
    # Per-area config blocks the skill needs/recommends (mirrors
    # AgentConfig per-area fields like `reflect`). Merged into
    # the host prompt's matching block on mount; prompt-side
    # keys win, skill-side keys fill in defaults.
    reflect: dict = field(default_factory=dict)
    body: str = ""


def load_skill(name: str) -> Skill:
    """Load a skill by short name. Raises ValueError if the file
    doesn't exist — surfaces typos in user-prompt `skills:`
    declarations at agent-init time rather than mid-run."""
    path = _SKILLS_DIR / f"{name}.md"
    if not path.exists():
        available = sorted(p.stem for p in _SKILLS_DIR.glob("*.md"))
        raise ValueError(
            f"skill not found: {name!r} (looked at {path}). "
            f"Available skills: {available or '(none — directory empty)'}"
        )
    fm = _fm_parse(path.read_text(encoding="utf-8"))
    meta = fm.meta or {}

    raw_tools = meta.get("tools", [])
    if not isinstance(raw_tools, list):
        raise ValueError(
            f"skill {name!r}: `tools` must be a list of strings, "
            f"got {type(raw_tools).__name__}"
        )
    tools = [str(t) for t in raw_tools if isinstance(t, str) and t.strip()]

    raw_extra = meta.get("extra_tools", [])
    if not isinstance(raw_extra, list):
        raise ValueError(
            f"skill {name!r}: `extra_tools` must be a list, "
            f"got {type(raw_extra).__name__}"
        )

    description = str(meta.get("description", "")).strip()
    raw_reflect = meta.get("reflect") or {}
    if not isinstance(raw_reflect, dict):
        raise ValueError(
            f"skill {name!r}: `reflect` must be a mapping, "
            f"got {type(raw_reflect).__name__}"
        )

    return Skill(
        name=name,
        description=description,
        tools=tools,
        extra_tools=list(raw_extra),
        reflect=dict(raw_reflect),
        body=fm.body.strip(),
    )


def mount_skills(skill_names: list[str], *, fm_meta: dict) -> str:
    """Load each named skill and merge its contributions into
    `fm_meta` in place. Returns the aggregated rationale body
    string for the `{skills}` placeholder.

    Mutations on `fm_meta`:
    - skill `tools` are appended to `fm_meta["tools"]` (registry
      dedupes by name; the host prompt's tools_field is always
      additive — base + user + skills all union).
    - skill `extra_tools` are appended to
      `fm_meta["extra_tools"]` (the existing register loop in
      Agent.__init__ picks them up).
    - skill per-area blocks (`reflect:`, ...) fill in
      `fm_meta[<area>]` per-key — prompt always wins, first
      skill in `skill_names` wins among skills (later skills
      don't silently override earlier ones). See
      AgentConfig.reflect for known keys.

    The returned body has a `## Skill: <name>` header per skill
    so the agent can mentally separate blocks when multiple
    skills are mounted. Empty string when no skills declared.
    """
    if not skill_names:
        return ""

    bodies: list[str] = []
    tools = list(fm_meta.get("tools") or [])
    extra_tools = list(fm_meta.get("extra_tools") or [])
    reflect = dict(fm_meta.get("reflect") or {})

    for name in skill_names:
        skill = load_skill(name)
        tools.extend(skill.tools)
        extra_tools.extend(skill.extra_tools)
        for k, v in skill.reflect.items():
            reflect.setdefault(k, v)
        if skill.body:
            bodies.append(f"## Skill: {name}\n\n{skill.body}")

    fm_meta["tools"] = tools
    fm_meta["extra_tools"] = extra_tools
    fm_meta["reflect"] = reflect

    return "\n\n".join(bodies)


def list_skills() -> list[str]:
    """Enumerate available skill names (alphabetical). Used in
    load_skill error messages for typo-spotting."""
    return sorted(p.stem for p in _SKILLS_DIR.glob("*.md"))


def list_skill_listings() -> list[dict]:
    """Catalog of skills with their public metadata. Analog of
    `AgentRegistry.to_listing` — each entry is `{name, description,
    tools, extra_tools}`. Consumed by the future
    `agent_list_skills()` discovery tool (TODO §17 Phase 4) and
    by anyone composing a `skills:` decision who needs more than
    just names to choose from."""
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "tools": list(skill.tools),
            "extra_tools": [t.get("name", "?") for t in skill.extra_tools],
            "reflect": dict(skill.reflect),
        }
        for skill in (load_skill(n) for n in list_skills())
    ]
