"""
Core dataclasses shared across all orchestra modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class NodeType(Enum):
    SINGLE = "single"
    REACT = "react"
    FAN_OUT = "fan_out"
    JOIN = "join"


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


# ── Fork / Auto-fork ─────────────────────────────────────────────────────────

@dataclass
class ForkConfig:
    enabled: bool = False
    mechanism: str = "tool"  # tool | sgr_auto | json_output
    max_forks: int = 2
    budget_split: str = "equal"  # equal | proportional | fixed
    context_handoff: str = "full_history"
    merge_strategy: str = "best_confidence"


@dataclass
class AutoForkConfig:
    enabled: bool = False
    trigger: str = "questions_remaining"
    threshold: int = 3
    strategy: str = "one_per_question"
    child_agent: str = ""
    context_handoff: str = "sgr_outcomes"
    max_children: int = 3
    depth_limit: int = 2


# ── Adaptive LLM params ──────────────────────────────────────────────────────

@dataclass
class ScheduleConfig:
    param: str  # temperature, top_p, frequency_penalty, etc.
    driver: str  # budget_ratio, sgr_confidence, repetition_score, custom, ...
    curve: str = "linear"  # linear, linear_decay, step, exponential_decay, inverse_linear, custom
    range: Optional[list[float]] = None  # [start, end]
    steps: Optional[dict[str, float]] = None  # for step curve: {"0.5": 0.3, ...}
    merge_policy: str = "last_wins"  # min, max, mean, last_wins
    handler: Optional[str] = None  # dotted path for custom driver/curve


@dataclass
class ModelScheduleEntry:
    phase: str  # tool_calls, reflection, synthesis, stuck
    model: str = ""
    condition: Optional[str] = None


@dataclass
class LLMParamsConfig:
    model: Optional[str] = None  # override agent-level default
    temperature: float = 0.3
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    max_completion_tokens: int = 4096
    schedules: list[ScheduleConfig] = field(default_factory=list)
    model_schedule: list[ModelScheduleEntry] = field(default_factory=list)


# ── Agent ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    name: str
    system_prompt: str = ""  # raw text or file path
    sgr: bool = False
    sgr_interval: int = 3
    sgr_extensions: Optional[dict[str, Any]] = None  # extra reflect() fields
    tools: list[str] = field(default_factory=list)
    spawn_tools: list[str] = field(default_factory=list)
    output_schema: Optional[Any] = None  # JSON Schema dict or shorthand
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    condensation: Optional[CondensationConfig] = None
    fork: Optional[ForkConfig] = None
    auto_fork: Optional[AutoForkConfig] = None
    llm_params: Optional[LLMParamsConfig] = None


# ── Topology ──────────────────────────────────────────────────────────────────

@dataclass
class NodeConfig:
    id: str
    agent: str = ""  # agent name or "$dynamic"
    type: NodeType = NodeType.REACT
    source: Optional[str] = None  # single upstream node
    sources: list[str] = field(default_factory=list)  # for join nodes
    parallel: bool = False
    spawn_from: Optional[str] = None  # dotted path for dynamic fan_out cardinality
    focus: Optional[str] = None  # prompt override / focus string


@dataclass
class EdgeConfig:
    from_node: str
    to_node: str
    context_handoff: str = "findings_only"


@dataclass
class FeedbackLoopConfig:
    observer: str  # agent name or "_runtime"
    targets: list[str] = field(default_factory=list)
    trigger: str = "on_child_reflect"
    condition: Optional[str] = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    max_feedback_budget_delta: Optional[int] = None


@dataclass
class TopologyConfig:
    name: str
    nodes: list[NodeConfig] = field(default_factory=list)
    edges: list[EdgeConfig] = field(default_factory=list)
    feedback_loops: list[FeedbackLoopConfig] = field(default_factory=list)
    shared_tools: dict[str, dict[str, Any]] = field(default_factory=dict)


# ── Top-level config ──────────────────────────────────────────────────────────

@dataclass
class OrchestraConfig:
    """Top-level config aggregating all sections."""
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    topologies: dict[str, TopologyConfig] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)  # YAML-declared tools
    shared_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    context_handoff_modes: dict[str, dict[str, Any]] = field(default_factory=dict)
    feedback_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Legacy / pass-through
    llm: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
