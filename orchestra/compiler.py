"""
LLM Compiler: reads prompt files, parses @headers, builds agent registry.

Two-pass parsing:
  1. Deterministic — regex extracts @headers (fast, no LLM cost)
  2. LLM fallback — for prompts without formal headers, an LLM infers metadata

The registry maps agent names to their metadata: summary, capabilities,
input schema, tools, budget, llm_params, and the prompt template.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .types import (
    AgentConfig,
    AgentMode,
    BudgetConfig,
    CondensationConfig,
    LLMParamsConfig,
    PusherConfig,
    PusherType,
)

log = logging.getLogger(__name__)

# ── Registry entry ────────────────────────────────────────────────────────────

@dataclass
class AgentRegistryEntry:
    """Compiled metadata for one agent."""
    name: str
    summary: str
    mode: AgentMode
    capabilities: list[str]
    tools: list[str]
    input_schema: dict[str, dict[str, str]]  # {field: {type, description}}
    budget: BudgetConfig
    llm_params: LLMParamsConfig
    sgr: bool
    sgr_interval: int
    prompt_template: str  # SYSTEM body with {placeholders} — stable across calls
    user_template: str = ""  # USER message template; empty → caller fills it
    guards: dict[str, str] = field(default_factory=dict)  # {trigger: message}
    source_file: str = ""
    source_hash: str = ""

    def to_agent_config(self) -> AgentConfig:
        """Convert to AgentConfig. tools is a single flat list — the
        framework registry knows which handler to call for each name."""
        tools = list(self.tools)

        # Backward compat: prompts that still declare framework features
        # via @capabilities (sgr, spawn, …) get translated into the
        # equivalent @tools entries. New prompts skip @capabilities.
        cap_to_tools = {
            "sgr": ["reflect"],
            "spawn": ["spawn_agent"],
            "list_agents": ["list_agents"],
        }
        for cap in self.capabilities:
            for t in cap_to_tools.get(cap, []):
                if t not in tools:
                    tools.append(t)

        return AgentConfig(
            name=self.name,
            system_prompt=self.prompt_template,
            user_prompt=self.user_template,
            mode=self.mode,
            sgr_interval=self.sgr_interval,
            tools=tools,
            budget=self.budget,
            llm_params=self.llm_params,
            input_schema=self.input_schema,
            guards=self.guards if self.guards else None,
        )

    def to_listing(self) -> dict:
        """Return summary for list_agents tool."""
        return {
            "name": self.name,
            "summary": self.summary,
            "mode": self.mode.value,
            "capabilities": self.capabilities,
            "tools": self.tools,
            "input_schema": self.input_schema,
        }


@dataclass
class AgentRegistry:
    """Compiled registry of all agents."""
    entries: dict[str, AgentRegistryEntry] = field(default_factory=dict)
    source_hash: str = ""

    def get(self, name: str) -> Optional[AgentRegistryEntry]:
        return self.entries.get(name)

    def get_config(self, name: str) -> Optional[AgentConfig]:
        entry = self.entries.get(name)
        return entry.to_agent_config() if entry else None

    def get_all_configs(self) -> dict[str, AgentConfig]:
        return {name: e.to_agent_config() for name, e in self.entries.items()}

    def to_listing(self) -> list[dict]:
        """Return all entries for list_agents tool."""
        return [e.to_listing() for e in self.entries.values()]

    def names(self) -> list[str]:
        return list(self.entries.keys())


# ── Compiler ──────────────────────────────────────────────────────────────────

_CACHE: dict[str, AgentRegistry] = {}


def compile_prompts(
    prompt_dir: str | Path,
    pattern: str = "*.md",
    llm: Any = None,
    model: str = "",
    use_cache: bool = True,
) -> AgentRegistry:
    """
    Compile prompt files into an agent registry.

    Args:
        prompt_dir: directory path, file:// URI, or bitbucket:// URI
        pattern: glob pattern for prompt files (used for local dirs)
        llm: optional LLM client for fallback parsing
        model: model name for LLM fallback
        use_cache: skip recompilation if files unchanged
    """
    uri = str(prompt_dir)

    # Resource URI (bitbucket://, explicit file://)
    if "://" in uri:
        return _compile_from_resource(uri, pattern, llm, model, use_cache)

    # Plain path (relative or absolute) — original behavior
    prompt_path = Path(prompt_dir)
    if not prompt_path.is_dir():
        log.warning("prompt directory does not exist: %s", prompt_path)
        return AgentRegistry()

    files = sorted(prompt_path.glob(pattern))
    if not files:
        return AgentRegistry()

    combined_hash = _hash_files(files)
    if use_cache and combined_hash in _CACHE:
        return _CACHE[combined_hash]

    registry = AgentRegistry(source_hash=combined_hash)
    log.info("compiling %d prompt files from %s", len(files), prompt_path)

    for filepath in files:
        # Skip sibling files: <name>.system.md / <name>.user.md hold
        # body fragments referenced by <name>.md, not standalone agents.
        stem = filepath.stem
        if stem.endswith(".system") or stem.endswith(".user"):
            continue
        try:
            text = filepath.read_text(encoding="utf-8")
            entry = _parse_prompt_file(text, filepath, llm, model)
            if entry:
                registry.entries[entry.name] = entry
                _log_compiled(entry)
            else:
                log.warning("  skipped %s — missing YAML frontmatter or @agent", filepath.name)
        except Exception as e:
            log.warning("  failed to compile %s: %s", filepath.name, e)

    if use_cache:
        _CACHE[combined_hash] = registry
    return registry


def _compile_from_resource(
    uri: str, pattern: str, llm: Any, model: str, use_cache: bool,
) -> AgentRegistry:
    """Compile prompts from a resource URI (file://, bitbucket://)."""
    from .resource import get_provider

    provider = get_provider(uri)
    file_uris = provider.list(uri)

    # Filter by pattern
    if pattern != "*":
        import fnmatch
        file_uris = [u for u in file_uris if fnmatch.fnmatch(u.split("/")[-1], pattern)]

    if not file_uris:
        log.warning("no prompt files found at %s", uri)
        return AgentRegistry()

    # Cache by URI + content hash
    contents = {}
    for file_uri in file_uris:
        try:
            contents[file_uri] = provider.get(file_uri)
        except Exception as e:
            log.warning("  failed to fetch %s: %s", file_uri, e)

    # Use provider's hash (commit SHA for bitbucket) or content hash as fallback
    combined_hash = provider.resolve_hash(uri)
    if not combined_hash:
        combined_hash = hashlib.md5("".join(sorted(contents.values())).encode()).hexdigest()
    if use_cache and combined_hash in _CACHE:
        return _CACHE[combined_hash]

    registry = AgentRegistry(source_hash=combined_hash)
    log.info("compiling %d prompt files from %s", len(contents), uri)

    for file_uri, text in contents.items():
        name = file_uri.split("/")[-1]
        try:
            entry = _parse_prompt_file(text, name, llm, model)
            if entry:
                registry.entries[entry.name] = entry
                _log_compiled(entry)
            else:
                log.warning("  skipped %s — no @agent header found", name)
        except Exception as e:
            log.warning("  failed to compile %s: %s", name, e)

    if use_cache:
        _CACHE[combined_hash] = registry
    return registry


def _log_compiled(entry) -> None:
    caps = ", ".join(entry.capabilities) if entry.capabilities else "none"
    tools = ", ".join(entry.tools[:5]) if entry.tools else "none"
    data_fields = ", ".join(entry.input_schema.keys()) if entry.input_schema else "none"
    log.info(
        "  compiled agent '%s' [%s] — capabilities=[%s] tools=[%s] data=[%s] budget=%dt/%ds",
        entry.name, entry.mode.value, caps, tools, data_fields,
        entry.budget.max_tokens, entry.budget.max_steps,
    )


def compile_prompt_text(
    text: str,
    name: str = "",
    source_file: str = "",
) -> Optional[AgentRegistryEntry]:
    """Compile a single prompt text into a registry entry."""
    return _parse_prompt_file(text, Path(source_file) if source_file else None)


# ── Prompt file parsing ───────────────────────────────────────────────────────

_HEADER_RE = re.compile(r"^@(\w+):\s*(.+)$", re.MULTILINE)
_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)
_USER_SEPARATOR = re.compile(r"^---\s*user\s*---\s*$", re.MULTILINE | re.IGNORECASE)


def _parse_prompt_file(
    text: str,
    filepath: Optional[Path] = None,
    llm: Any = None,
    model: str = "",
) -> Optional[AgentRegistryEntry]:
    """Parse an agent prompt file. Format: YAML frontmatter + body.

    File starts with `---\\n`, a YAML metadata block, then a closing
    `---\\n`, then the system message body. Standard frontmatter
    pattern used by Jekyll, Hugo, Obsidian, MDX.

    The body is the system prompt. The user-message template either
    follows the body after a `--- user ---` separator, or lives in a
    sibling file `<name>.user.md`. The system body itself can also be
    extracted to a sibling `<name>.system.md`.
    """
    import yaml as _yaml

    parts = _SEPARATOR.split(text.lstrip(), maxsplit=1)
    if len(parts) != 2 or parts[0].strip() != "":
        return None  # missing opening `---`
    after_first = parts[1]
    closing = _SEPARATOR.split(after_first, maxsplit=1)
    if len(closing) != 2:
        return None  # missing closing `---`
    yaml_block, body = closing
    try:
        yaml_headers = _yaml.safe_load(yaml_block) or {}
    except Exception:
        return None
    if not isinstance(yaml_headers, dict):
        return None

    # Optional second split: a `--- user ---` line inside the body
    # separates the SYSTEM portion (stable methodology, tool docs,
    # behavioural rules — cacheable) from a USER message template
    # (current task / trigger framing — varies per call). Without
    # this marker the whole body is treated as system, the user
    # message defaults to "Begin." (existing behaviour).
    user_template = ""
    user_parts = _USER_SEPARATOR.split(body, maxsplit=1)
    if len(user_parts) == 2:
        body, user_template = user_parts

    # Convention-based external override: if `<name>.system.md` /
    # `<name>.user.md` exist next to the file, they REPLACE whatever
    # the body parsed above. Lets prompt authors keep methodology and
    # per-call template in their own files without ceremony — the
    # main file becomes a tiny YAML-frontmatter metadata block.
    if filepath is not None:
        sib_system = filepath.with_name(f"{filepath.stem}.system.md")
        sib_user = filepath.with_name(f"{filepath.stem}.user.md")
        if sib_system.exists():
            body = sib_system.read_text(encoding="utf-8")
        if sib_user.exists():
            user_template = sib_user.read_text(encoding="utf-8")

    headers, input_schema, guards = _from_yaml_headers(yaml_headers)

    if not headers.get("agent"):
        if filepath:
            headers["agent"] = filepath.stem
        else:
            return None

    # Pass 2: LLM fallback for missing fields
    if llm and (not headers.get("summary") or not headers.get("capabilities")):
        llm_meta = _llm_extract_metadata(text, llm, model)
        for key, val in llm_meta.items():
            if key not in headers or not headers[key]:
                headers[key] = val

    # Build entry
    name = headers.get("agent", "")
    mode = AgentMode(headers.get("mode", "react"))
    capabilities = _parse_list(headers.get("capabilities", ""))
    tools = _parse_list(headers.get("tools", ""))
    budget = _parse_budget_header(headers.get("budget", ""))
    llm_params = _parse_llm_header(headers.get("llm", ""))
    sgr = "sgr" in capabilities
    sgr_interval = int(headers.get("sgr_interval", "3"))
    summary = headers.get("summary", "").strip()

    return AgentRegistryEntry(
        name=name,
        summary=summary,
        mode=mode,
        capabilities=capabilities,
        tools=tools,
        input_schema=input_schema,
        guards=guards,
        budget=budget,
        llm_params=llm_params,
        sgr=sgr,
        sgr_interval=sgr_interval,
        prompt_template=body.strip(),
        user_template=user_template.strip(),
        source_file=str(filepath) if filepath else "",
        source_hash=hashlib.md5(text.encode()).hexdigest()[:8],
    )


def _from_yaml_headers(y: dict) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    """Convert YAML-frontmatter dict into the (headers, input_schema, guards)
    triple the rest of the parser expects.

    The legacy @header parser produces flat string values; YAML can
    carry richer structures. We adapt:
      - tools: list → comma-joined string for `_parse_list`
      - capabilities: list → comma-joined
      - budget: dict {tokens, steps, wall} → header string
        "Ntokens, Msteps[, Ws]"
      - llm: dict {temperature, top_p, …} → header string
        "key=value, key=value"
      - data: dict {field: {type, description, from}} → input_schema
      - guards: dict {trigger: message} → guards
      - summary: string (multi-line OK)
    """
    h: dict[str, str] = {}

    if "agent" in y:    h["agent"] = str(y["agent"])
    if "mode" in y:     h["mode"] = str(y["mode"])
    if "summary" in y:  h["summary"] = str(y["summary"]).strip()
    if "sgr_interval" in y: h["sgr_interval"] = str(y["sgr_interval"])

    raw_tools = y.get("tools") or []
    if isinstance(raw_tools, list):
        h["tools"] = ", ".join(str(t).strip() for t in raw_tools)
    else:
        h["tools"] = str(raw_tools)

    raw_caps = y.get("capabilities") or []
    if isinstance(raw_caps, list):
        h["capabilities"] = ", ".join(str(c).strip() for c in raw_caps)
    else:
        h["capabilities"] = str(raw_caps)

    # budget: either inline string ("50000 tokens, 50 steps") or dict
    bud = y.get("budget")
    if isinstance(bud, str):
        h["budget"] = bud
    elif isinstance(bud, dict):
        parts: list[str] = []
        if "tokens" in bud:
            parts.append(f"{int(bud['tokens'])} tokens")
        if "steps" in bud:
            parts.append(f"{int(bud['steps'])} steps")
        if "wall" in bud:
            parts.append(f"{bud['wall']}")
        h["budget"] = ", ".join(parts)

    # llm: dict {temperature, top_p, ...} → key=value pairs
    lm = y.get("llm")
    if isinstance(lm, str):
        h["llm"] = lm
    elif isinstance(lm, dict):
        h["llm"] = ", ".join(f"{k}={v}" for k, v in lm.items())

    # data: dict {field: {type, description, from}}
    # `from` is "tool.field" — split into from_tool / from_field for
    # the data-provider resolver in orchestra/agent.py.
    input_schema: dict[str, dict[str, str]] = {}
    for field_name, spec in (y.get("data") or {}).items():
        if isinstance(spec, dict):
            entry: dict[str, str] = {}
            for k in ("type", "description"):
                if k in spec:
                    entry[k] = str(spec[k])
            from_v = spec.get("from")
            if from_v and isinstance(from_v, str) and "." in from_v:
                tool, _, field_id = from_v.partition(".")
                entry["from_tool"] = tool
                entry["from_field"] = field_id
            input_schema[str(field_name)] = entry
        elif isinstance(spec, str):
            input_schema[str(field_name)] = {"type": spec}

    # guards: dict {trigger: message}
    guards: dict[str, str] = {}
    for trigger, msg in (y.get("guards") or {}).items():
        guards[str(trigger)] = str(msg)

    return h, input_schema, guards


def _parse_list(value: str) -> list[str]:
    """Parse comma-separated list."""
    if not value:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _parse_budget_header(value: str) -> BudgetConfig:
    """Parse '@budget: 40000 tokens, 40 steps, 180s'."""
    if not value:
        return BudgetConfig()

    tokens = 40_000
    steps = 40
    wall_time = None

    parts = [p.strip().lower() for p in value.split(",")]
    for part in parts:
        if "token" in part:
            num = re.search(r"(\d+)", part)
            if num:
                tokens = int(num.group(1))
        elif "step" in part:
            num = re.search(r"(\d+)", part)
            if num:
                steps = int(num.group(1))
        elif part.endswith("s") or part.endswith("m"):
            num = re.search(r"([\d.]+)", part)
            if num:
                val = float(num.group(1))
                wall_time = val * 60 if part.endswith("m") else val

    from .prompts import load_internal
    return BudgetConfig(
        max_tokens=tokens,
        max_steps=steps,
        max_wall_time=wall_time,
        pushers=[
            PusherConfig(at=0.75, type=PusherType.NUDGE,
                         message=load_internal("pushers/nudge")),
            PusherConfig(at=1.0, type=PusherType.FORCE_DONE),
        ],
    )


def _parse_llm_header(value: str) -> LLMParamsConfig:
    """Parse '@llm: model=gpt-4o temperature=0.3 top_p=1.0'."""
    if not value:
        return LLMParamsConfig()

    params = LLMParamsConfig()
    for pair in value.split():
        if "=" in pair:
            key, val = pair.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key == "model":
                params.model = val
            elif key == "temperature":
                params.temperature = float(val)
            elif key == "top_p":
                params.top_p = float(val)
            elif key == "frequency_penalty":
                params.frequency_penalty = float(val)
            elif key == "presence_penalty":
                params.presence_penalty = float(val)
            elif key == "max_completion_tokens":
                params.max_completion_tokens = int(val)
            elif key == "tool_choice":
                params.tool_choice = val

    return params


# ── LLM fallback ──────────────────────────────────────────────────────────────

def _llm_extract_metadata(text: str, llm: Any, model: str) -> dict[str, str]:
    """Use LLM to extract metadata from a prompt without formal @headers."""
    try:
        resp = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Extract metadata from this agent prompt. Return JSON only:\n"
                    '{"summary": "1-3 sentence description", '
                    '"capabilities": "comma-separated: sgr, spawn, plan, fork, etc.", '
                    '"tools": "comma-separated tool names used", '
                    '"mode": "single or react"}'
                )},
                {"role": "user", "content": text[:3000]},
            ],
            temperature=0,
            stream=False,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except Exception as e:
        log.warning("LLM metadata extraction failed: %s", e)
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_files(files: list[Path]) -> str:
    """Combined hash of all file contents."""
    h = hashlib.md5()
    for f in sorted(files):
        h.update(f.read_bytes())
    return h.hexdigest()
