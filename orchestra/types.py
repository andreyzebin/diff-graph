"""
Core dataclasses shared across all orchestra modules.

Simplified: no topologies, no adaptive schedules, no feedback loop configs.
Two agent modes (single + react), tools, budget, SGR, mutable params.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class AgentMode(Enum):
    SINGLE = "single"
    REACT = "react"


class PusherType(Enum):
    NUDGE = "nudge"
    FORCE_REFLECT = "force_reflect"
    FORCE_DONE = "force_done"
    CUSTOM = "custom"


class CondensationStrategy(Enum):
    LLM_SUMMARY = "llm_summary"
    SLIDING_WINDOW = "sliding_window"
    DROP_TOOL_RESULTS = "drop_tool_results"
    HYBRID = "hybrid"


# ── Budget ────────────────────────────────────────────────────────────────────

@dataclass
class PusherConfig:
    at: float  # 0.0–1.0
    type: PusherType = PusherType.NUDGE
    message: str = ""
    handler: Optional[str] = None  # dotted path for custom


@dataclass
class BudgetConfig:
    max_tokens: int = 40_000
    max_steps: int = 40
    max_wall_time: Optional[float] = None  # seconds
    max_children_budget: float = 0.3
    max_feedback_budget_delta: int = 10  # max steps a supervisor can extend
    cache_discount: float = 0.1  # cached tokens cost this fraction (0.1 = 90% cheaper)
    pushers: list[PusherConfig] = field(default_factory=list)


# ── Condensation ──────────────────────────────────────────────────────────────

@dataclass
class CondensationConfig:
    enabled: bool = True
    trigger: int = 30_000  # token threshold
    strategy: CondensationStrategy = CondensationStrategy.LLM_SUMMARY
    preserve_last: int = 5
    preserve_sgr: bool = True
    condense_prompt: str = "Summarize the conversation so far in <500 words."


# ── LLM Params (mutable at runtime) ──────────────────────────────────────────

@dataclass
class LLMParamsConfig:
    """Initial LLM params. These become mutable state on the agent at runtime."""
    model: Optional[str] = None
    temperature: float = 0.3
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    max_completion_tokens: int = 4096


# ── Agent ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    name: str
    system_prompt: str = ""  # raw text or file path
    mode: AgentMode = AgentMode.REACT  # single | react
    sgr: bool = False
    sgr_interval: int = 3
    sgr_extensions: Optional[dict[str, Any]] = None  # extra reflect() fields
    tools: list[str] = field(default_factory=list)  # domain tools
    meta_tools: list[str] = field(default_factory=list)  # spawn_agent, spawn_many, plan, fork, adjust_agent, observe_agents
    output_schema: Optional[Any] = None  # JSON Schema for done() output
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    condensation: Optional[CondensationConfig] = None
    llm_params: Optional[LLMParamsConfig] = None
    max_depth: int = 3  # max spawn/fork depth


# ── Top-level config ──────────────────────────────────────────────────────────

@dataclass
class OrchestraConfig:
    """Top-level config: agents, tools, shared tools."""
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)  # YAML-declared tools
    shared_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Legacy / pass-through for diffgraph
    llm: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
