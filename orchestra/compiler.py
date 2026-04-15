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
    prompt_template: str  # body with {placeholders}
    guards: dict[str, str] = field(default_factory=dict)  # {trigger: message}
    source_file: str = ""
    source_hash: str = ""

    def to_agent_config(self) -> AgentConfig:
        """Convert to AgentConfig for agent instantiation."""
        meta_tools = []
        cap_to_meta = {
            "sgr": [],  # handled by sgr flag
            "spawn": ["spawn_agent"],
            "spawn_many": ["spawn_many"],
            "plan": ["plan"],
            "fork": ["fork"],
            "adjust_agent": ["adjust_agent"],
            "observe_agents": ["observe_agents"],
            "list_agents": ["list_agents"],
        }
        for cap in self.capabilities:
            meta_tools.extend(cap_to_meta.get(cap, []))

        return AgentConfig(
            name=self.name,
            system_prompt=self.prompt_template,
            mode=self.mode,
            sgr=self.sgr,
            sgr_interval=self.sgr_interval,
            tools=list(self.tools),
            meta_tools=meta_tools,
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
    pattern: str = "*.prompt",
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
        try:
            text = filepath.read_text(encoding="utf-8")
            entry = _parse_prompt_file(text, filepath, llm, model)
            if entry:
                registry.entries[entry.name] = entry
                _log_compiled(entry)
            else:
                log.warning("  skipped %s — no @agent header found", filepath.name)
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
_DATA_LINE_RE = re.compile(r"^\s+(\w+):\s*(\w+)\s*[—–-]\s*(.+)$")
_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_prompt_file(
    text: str,
    filepath: Optional[Path] = None,
    llm: Any = None,
    model: str = "",
) -> Optional[AgentRegistryEntry]:
    """Parse a prompt file: extract @headers + body."""

    # Split header and body on ---
    parts = _SEPARATOR.split(text, maxsplit=1)
    if len(parts) == 2:
        header_text, body = parts
    else:
        # No separator — try to parse headers from full text
        header_text = text
        body = text

    # Pass 1: deterministic header extraction
    headers = _extract_headers(header_text)

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
    input_schema = _parse_data_section(header_text)
    guards = _parse_guards_section(header_text)
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
        source_file=str(filepath) if filepath else "",
        source_hash=hashlib.md5(text.encode()).hexdigest()[:8],
    )


def _extract_headers(text: str) -> dict[str, str]:
    """Extract @key: value pairs from header section."""
    headers: dict[str, str] = {}
    current_key: Optional[str] = None
    current_value: list[str] = []

    for line in text.splitlines():
        match = re.match(r"^@(\w+):\s*(.*)", line)
        if match:
            # Save previous
            if current_key:
                headers[current_key] = "\n".join(current_value).strip()
            current_key = match.group(1)
            current_value = [match.group(2)]
        elif current_key and (line.startswith("  ") or line.startswith("\t")):
            # Continuation line
            current_value.append(line)
        else:
            # End of headers
            if current_key:
                headers[current_key] = "\n".join(current_value).strip()
                current_key = None
                current_value = []

    if current_key:
        headers[current_key] = "\n".join(current_value).strip()

    return headers


_FROM_RE = re.compile(r"from:(\w+)\.(\w+)")


def _parse_data_section(header_text: str) -> dict[str, dict[str, str]]:
    """
    Parse @data section into input schema.

    Supports from:tool.field syntax in descriptions:
      diff_summary: string -- from:pr_context.diff_summary
    """
    schema: dict[str, dict[str, str]] = {}
    in_data = False

    for line in header_text.splitlines():
        if line.strip().startswith("@data:"):
            in_data = True
            continue
        if in_data:
            if line.startswith("@") or (line.strip() and not line.startswith(" ") and not line.startswith("\t")):
                break
            match = _DATA_LINE_RE.match(line)
            if match:
                field_name = match.group(1)
                field_type = match.group(2)
                field_desc = match.group(3).strip().strip('"\'')
                entry: dict[str, str] = {"type": field_type, "description": field_desc}
                # Parse from:tool.field
                from_match = _FROM_RE.search(field_desc)
                if from_match:
                    entry["from_tool"] = from_match.group(1)
                    entry["from_field"] = from_match.group(2)
                schema[field_name] = entry

    return schema


_GUARD_LINE_RE = re.compile(r'^\s+(\w+):\s*"(.+)"$')


def _parse_guards_section(header_text: str) -> dict[str, str]:
    """Parse @guards section: trigger_name: "message"."""
    guards: dict[str, str] = {}
    in_guards = False
    for line in header_text.splitlines():
        if line.strip().startswith("@guards:"):
            in_guards = True
            continue
        if in_guards:
            if line.startswith("@") or (line.strip() and not line.startswith(" ") and not line.startswith("\t")):
                break
            match = _GUARD_LINE_RE.match(line)
            if match:
                guards[match.group(1)] = match.group(2)
    return guards


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

    return BudgetConfig(
        max_tokens=tokens,
        max_steps=steps,
        max_wall_time=wall_time,
        pushers=[
            PusherConfig(at=0.75, type=PusherType.NUDGE,
                         message="75% budget used. Wrap up current investigation and call done()."),
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
