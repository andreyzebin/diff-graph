"""
YAML config loading, environment variable expansion, and validation.
Simplified: no topologies, no feedback loops, no adaptive schedules.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .types import (
    AgentConfig,
    AgentMode,
    BudgetConfig,
    CondensationConfig,
    CondensationStrategy,
    LLMParamsConfig,
    OrchestraConfig,
    PusherConfig,
    PusherType,
)

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


# ── Public API ────────────────────────────────────────────────────────────────

def load_config(path: str | Path, local_path: str | Path | None = None) -> OrchestraConfig:
    """Load YAML config, optionally deep-merge a local override, expand env vars."""
    base = _load_yaml(Path(path))
    if local_path and Path(local_path).exists():
        local = _load_yaml(Path(local_path))
        base = _deep_merge(base, local)
    base = _expand_env_vars(base)
    return _parse_config(base)


def merge_configs(base: OrchestraConfig, overrides: dict[str, Any]) -> OrchestraConfig:
    """Apply a dict of overrides onto an existing config."""
    raw = _config_to_raw(base)
    raw = _deep_merge(raw, overrides)
    return _parse_config(raw)


def validate_config(config: OrchestraConfig) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors: list[str] = []
    for name, agent in config.agents.items():
        for p in agent.budget.pushers:
            if not 0.0 <= p.at <= 1.0:
                errors.append(f"agent '{name}': pusher 'at' must be 0.0–1.0, got {p.at}")
    return errors


def resolve_prompt(prompt_ref: str, base_dir: Path | None = None) -> str:
    """If prompt_ref is a file path that exists, load it; otherwise return as-is."""
    if not prompt_ref:
        return ""
    if base_dir:
        candidate = base_dir / prompt_ref
    else:
        candidate = Path(prompt_ref)
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return prompt_ref


# ── Internal ──────────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _expand_env_vars(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(i) for i in obj]
    return obj


def _parse_config(raw: dict) -> OrchestraConfig:
    agents = {}
    for name, adict in raw.get("agents", {}).items():
        agents[name] = _parse_agent(name, adict)

    return OrchestraConfig(
        agents=agents,
        tools=raw.get("tools", {}),
        shared_tools=raw.get("shared_tools", {}),
        llm=raw.get("llm", {}),
        review=raw.get("review", {}),
    )


def _parse_agent(name: str, d: dict) -> AgentConfig:
    budget_raw = d.get("budget", {})
    pushers = [
        PusherConfig(
            at=p.get("at", 0),
            type=PusherType(p.get("type", "nudge")),
            message=p.get("message", ""),
            handler=p.get("handler"),
        )
        for p in budget_raw.get("pushers", [])
    ]
    budget = BudgetConfig(
        max_tokens=budget_raw.get("max_tokens", 40_000),
        max_steps=budget_raw.get("max_steps", 40),
        max_wall_time=_parse_duration(budget_raw.get("max_wall_time")),
        max_children_budget=budget_raw.get("max_children_budget", 0.3),
        max_feedback_budget_delta=budget_raw.get("max_feedback_budget_delta", 10),
        pushers=pushers,
    )

    condensation = None
    if "condensation" in d:
        c = d["condensation"]
        condensation = CondensationConfig(
            enabled=c.get("enabled", True),
            trigger=c.get("trigger", 30_000),
            strategy=CondensationStrategy(c.get("strategy", "llm_summary")),
            preserve_last=c.get("preserve_last", 5),
            preserve_sgr=c.get("preserve_sgr", True),
            condense_prompt=c.get("condense_prompt", CondensationConfig.condense_prompt),
        )

    llm_params = None
    if "llm_params" in d:
        lp = d["llm_params"]
        llm_params = LLMParamsConfig(
            model=lp.get("model"),
            temperature=lp.get("temperature", 0.3),
            top_p=lp.get("top_p", 1.0),
            frequency_penalty=lp.get("frequency_penalty", 0.0),
            presence_penalty=lp.get("presence_penalty", 0.0),
            max_completion_tokens=lp.get("max_completion_tokens", 4096),
            tool_choice=lp.get("tool_choice", "required"),
        )

    return AgentConfig(
        name=name,
        system_prompt=d.get("system_prompt", ""),
        mode=AgentMode(d.get("mode", "react")),
        sgr=d.get("sgr", False),
        sgr_interval=d.get("sgr_interval", 3),
        sgr_extensions=d.get("sgr_extensions"),
        tools=d.get("tools", []),
        meta_tools=d.get("meta_tools", []),
        output_schema=d.get("output_schema"),
        budget=budget,
        condensation=condensation,
        llm_params=llm_params,
        max_depth=d.get("max_depth", 3),
    )


def _parse_duration(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    return float(s)


def _config_to_raw(config: OrchestraConfig) -> dict:
    import dataclasses
    from enum import Enum
    def _to_dict(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: _to_dict(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_to_dict(i) for i in obj]
        return obj
    return _to_dict(config)
