"""
YAML config loading, environment variable expansion, and validation.
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
    AutoForkConfig,
    AutoSpawnConfig,
    BudgetConfig,
    CondensationConfig,
    CondensationStrategy,
    EdgeConfig,
    FeedbackLoopConfig,
    ForkConfig,
    LLMParamsConfig,
    ModelScheduleEntry,
    NodeConfig,
    NodeType,
    OrchestraConfig,
    PusherConfig,
    PusherType,
    ScheduleConfig,
    TopologyConfig,
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
    agent_names = set(config.agents.keys())

    for name, topo in config.topologies.items():
        node_ids = {n.id for n in topo.nodes}
        for node in topo.nodes:
            if node.agent != "$dynamic" and node.agent and node.agent not in agent_names:
                errors.append(f"topology '{name}' node '{node.id}': unknown agent '{node.agent}'")
            if node.source and node.source not in node_ids:
                errors.append(f"topology '{name}' node '{node.id}': unknown source '{node.source}'")
            for s in node.sources:
                if s not in node_ids:
                    errors.append(f"topology '{name}' node '{node.id}': unknown source '{s}'")
        for edge in topo.edges:
            if edge.from_node not in node_ids:
                errors.append(f"topology '{name}' edge: unknown from_node '{edge.from_node}'")
            if edge.to_node not in node_ids:
                errors.append(f"topology '{name}' edge: unknown to_node '{edge.to_node}'")

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

    topologies = {}
    for name, tdict in raw.get("topologies", {}).items():
        topologies[name] = _parse_topology(name, tdict)

    return OrchestraConfig(
        agents=agents,
        topologies=topologies,
        tools=raw.get("tools", {}),
        shared_tools=raw.get("shared_tools", {}),
        context_handoff_modes=raw.get("context_handoff_modes", {}),
        feedback_signals=raw.get("feedback_signals", {}),
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

    fork = None
    if "fork" in d:
        f = d["fork"]
        fork = ForkConfig(
            enabled=f.get("enabled", False),
            mechanism=f.get("mechanism", "tool"),
            max_forks=f.get("max_forks", 2),
            budget_split=f.get("budget_split", "equal"),
            context_handoff=f.get("context_handoff", "full_history"),
            merge_strategy=f.get("merge_strategy", "best_confidence"),
        )

    auto_fork = None
    if "auto_fork" in d:
        af = d["auto_fork"]
        auto_fork = AutoForkConfig(
            enabled=af.get("enabled", False),
            trigger=af.get("trigger", "questions_remaining"),
            threshold=af.get("threshold", 3),
            strategy=af.get("strategy", "one_per_question"),
            child_agent=af.get("child_agent", ""),
            context_handoff=af.get("context_handoff", "sgr_outcomes"),
            max_children=af.get("max_children", 3),
            depth_limit=af.get("depth_limit", 2),
        )

    llm_params = None
    if "llm_params" in d:
        lp = d["llm_params"]
        schedules = [
            ScheduleConfig(
                param=s["param"],
                driver=s.get("driver", "budget_ratio"),
                curve=s.get("curve", "linear"),
                range=s.get("range"),
                steps={str(k): v for k, v in s["steps"].items()} if s.get("steps") else None,
                merge_policy=s.get("merge_policy", "last_wins"),
                handler=s.get("handler"),
            )
            for s in lp.get("schedules", [])
        ]
        model_schedule = [
            ModelScheduleEntry(
                phase=ms["phase"],
                model=ms.get("model", ""),
                condition=ms.get("condition"),
            )
            for ms in lp.get("model_schedule", [])
        ]
        llm_params = LLMParamsConfig(
            model=lp.get("model"),
            temperature=lp.get("temperature", 0.3),
            top_p=lp.get("top_p", 1.0),
            frequency_penalty=lp.get("frequency_penalty", 0.0),
            presence_penalty=lp.get("presence_penalty", 0.0),
            max_completion_tokens=lp.get("max_completion_tokens", 4096),
            schedules=schedules,
            model_schedule=model_schedule,
        )

    auto_spawn = []
    for asp in d.get("auto_spawn", []):
        auto_spawn.append(AutoSpawnConfig(
            enabled=asp.get("enabled", False),
            trigger=asp.get("trigger", "on_start"),
            spawn_type=asp.get("spawn_type", "plan"),
            threshold=asp.get("threshold", 0),
            max_spawns=asp.get("max_spawns", 3),
            child_agent=asp.get("child_agent", ""),
            context_handoff=asp.get("context_handoff", "sgr_outcomes"),
            plan_prompt=asp.get("plan_prompt", ""),
            inject_result=asp.get("inject_result", True),
        ))

    return AgentConfig(
        name=name,
        system_prompt=d.get("system_prompt", ""),
        sgr=d.get("sgr", False),
        sgr_interval=d.get("sgr_interval", 3),
        sgr_extensions=d.get("sgr_extensions"),
        tools=d.get("tools", []),
        spawn_tools=d.get("spawn_tools", []),
        output_schema=d.get("output_schema"),
        budget=budget,
        condensation=condensation,
        fork=fork,
        auto_fork=auto_fork,
        auto_spawn=auto_spawn,
        llm_params=llm_params,
    )


def _parse_topology(name: str, d: dict) -> TopologyConfig:
    nodes = []
    for nd in d.get("nodes", []):
        nodes.append(NodeConfig(
            id=nd["id"],
            agent=nd.get("agent", ""),
            type=NodeType(nd.get("type", "react")),
            source=nd.get("source"),
            sources=nd.get("sources", []),
            parallel=nd.get("parallel", False),
            spawn_from=nd.get("spawn_from"),
            focus=nd.get("focus"),
        ))

    edges = []
    for ed in d.get("edges", []):
        edges.append(EdgeConfig(
            from_node=ed.get("from", ""),
            to_node=ed.get("to", ""),
            context_handoff=ed.get("context_handoff", "findings_only"),
        ))

    feedback_loops = []
    for fl in d.get("feedback_loops", []):
        feedback_loops.append(FeedbackLoopConfig(
            observer=fl.get("observer", "_runtime"),
            targets=fl.get("targets", []),
            trigger=fl.get("trigger", "on_child_reflect"),
            condition=fl.get("condition"),
            actions=fl.get("actions", []),
            max_feedback_budget_delta=fl.get("max_feedback_budget_delta"),
        ))

    return TopologyConfig(
        name=name,
        nodes=nodes,
        edges=edges,
        feedback_loops=feedback_loops,
        shared_tools=d.get("shared_tools", {}),
    )


def _parse_duration(val: Any) -> float | None:
    """Parse '120s', '5m', or numeric seconds."""
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
    """Rough conversion back to raw dict for merging."""
    import dataclasses
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
