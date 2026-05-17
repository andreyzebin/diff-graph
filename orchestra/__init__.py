"""
Orchestra — agent-first orchestration framework.

Two agent modes (single + react), tool system, mutable LLM params,
SGR, budget constraints, and observability. No topologies, no pipelines.
All structure created at runtime by agents via tools.
"""
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
from .config import load_config, merge_configs, validate_config, resolve_prompt
from .events import EventBus, EventType, OnEvent
from .agent import Agent, AgentResult, AgentTrace
from .budget import BudgetTracker, BudgetState
from .compiler import compile_prompts, compile_prompt_text, AgentRegistry, AgentRegistryEntry
from .tools.registry import ToolRegistry

__all__ = [
    # Types
    "AgentConfig", "AgentMode", "BudgetConfig", "CondensationConfig",
    "CondensationStrategy", "LLMParamsConfig", "OrchestraConfig",
    "PusherConfig", "PusherType",
    # Config
    "load_config", "merge_configs", "validate_config", "resolve_prompt",
    # Events
    "EventBus", "EventType", "OnEvent",
    # Core
    "Agent", "AgentResult", "AgentTrace",
    "BudgetTracker", "BudgetState",
    "ToolRegistry",
    # Compiler
    "compile_prompts", "compile_prompt_text", "AgentRegistry", "AgentRegistryEntry",
]
