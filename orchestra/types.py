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
    tool_choice: str = "required"  # "required" | "auto"
    stream: bool = True              # set False for backends with broken
                                     # streaming tool-call parsers (e.g. some
                                     # vLLM Qwen3-Coder deployments)
    extra_body: Optional[dict] = None  # vendor extensions, e.g.
                                       # {"chat_template_kwargs": {"enable_thinking": False}}


# ── Agent ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    name: str
    # Static methodology / tool docs / behavioural rules. NEVER carries
    # per-call placeholders ({focus}, {message}, {diff_summary}, …) so it
    # hits the LLM provider's prompt cache verbatim across all calls of
    # the same agent.
    system_prompt: str = ""
    # User-message template. Carries all per-call interpolation —
    # placeholders here are filled at run time from the agent's data
    # scope. Empty string means "no default user message"; the
    # framework falls back to "Begin." (production) or whatever the
    # caller injected (tests / parent spawn).
    user_prompt: str = ""
    mode: AgentMode = AgentMode.REACT  # single | react
    # Step-cadence for the reflect pusher: every `reflect_interval`
    # tool-using steps without reflect, the framework injects a NUDGE;
    # at 2× the interval it narrows tools_schema to reflect-only.
    # Counter resets when reflect actually fires.
    reflect_interval: int = 3
    sgr_extensions: Optional[dict[str, Any]] = None  # extra reflect() fields
    # Every tool the agent can call — domain (post_comment, read_file, …)
    # and framework (spawn_agent, reflect, list_agents). The presence of
    # `reflect` here is what we used to call SGR; consumers check for it
    # directly rather than via a separate flag.
    tools: list[str] = field(default_factory=list)
    output_schema: Optional[Any] = None  # JSON Schema for done() output
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    condensation: Optional[CondensationConfig] = None
    llm_params: Optional[LLMParamsConfig] = None
    max_depth: int = 3  # max spawn depth
    input_schema: Optional[dict] = None  # @data fields with from: metadata
    guards: Optional[dict[str, str]] = None  # {trigger_name: message} from @guards


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
