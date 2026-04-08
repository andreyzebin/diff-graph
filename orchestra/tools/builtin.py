"""
Builtin tools: reflect, done, spawn_agent, fork, create_topology.

These are registered automatically based on agent config flags.
Each is a closure factory that captures the agent's runtime state.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, TYPE_CHECKING

from .registry import ToolDef, ToolRegistry

if TYPE_CHECKING:
    from ..agent import Agent
    from ..sgr import SGRTracker


def register_builtins(
    registry: ToolRegistry,
    agent_config: Any,
    sgr_tracker: Optional["SGRTracker"] = None,
    done_callback: Optional[Callable[[Any], None]] = None,
    spawn_callback: Optional[Callable[[dict], Any]] = None,
    fork_callback: Optional[Callable[[dict], Any]] = None,
) -> None:
    """Register builtin tools based on agent config flags."""

    # ── reflect ───────────────────────────────────────────────────────────
    if agent_config.sgr and sgr_tracker:
        schema = sgr_tracker.build_reflect_schema()
        registry.register_tool_def(ToolDef(
            name="reflect",
            description=(
                "Structured self-reflection. Call every 3-5 steps to track progress, "
                "avoid going in circles, and plan the next action."
            ),
            parameters=schema,
            handler=_make_reflect_handler(sgr_tracker),
            is_builtin=True,
        ))

    # ── done ──────────────────────────────────────────────────────────────
    done_params: dict[str, Any] = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Array of structured findings.",
            },
        },
        "required": ["findings"],
    }
    # Use output_schema if provided
    if agent_config.output_schema and isinstance(agent_config.output_schema, dict):
        done_params = {
            "type": "object",
            "properties": {"findings": agent_config.output_schema},
            "required": ["findings"],
        }

    registry.register_tool_def(ToolDef(
        name="done",
        description="Submit all findings and stop.",
        parameters=done_params,
        handler=_make_done_handler(done_callback),
        is_builtin=True,
    ))

    # ── spawn_agent ───────────────────────────────────────────────────────
    if agent_config.spawn_tools and "spawn_agent" in agent_config.spawn_tools:
        registry.register_tool_def(ToolDef(
            name="spawn_agent",
            description=(
                "Spawn a sub-agent to investigate a specific question. "
                "The sub-agent runs independently and returns its findings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Name of the agent config to spawn.",
                    },
                    "focus": {
                        "type": "string",
                        "description": "What this sub-agent should investigate.",
                    },
                    "context_handoff": {
                        "type": "string",
                        "description": "How to pass context: sgr_outcomes, full_history, findings_only.",
                        "enum": ["sgr_outcomes", "full_history", "findings_only"],
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "Wait for the sub-agent to complete before continuing.",
                    },
                },
                "required": ["agent", "focus"],
            },
            handler=_make_spawn_handler(spawn_callback),
            is_builtin=True,
        ))

    # ── fork ──────────────────────────────────────────────────────────────
    if agent_config.fork and agent_config.fork.enabled:
        registry.register_tool_def(ToolDef(
            name="fork",
            description=(
                "Fork yourself into parallel branches, each pursuing a different hypothesis. "
                "Results will be merged when all branches complete."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "branches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "focus": {"type": "string", "description": "What this branch should investigate."},
                            },
                            "required": ["focus"],
                        },
                        "description": "List of branches to explore in parallel.",
                    },
                },
                "required": ["branches"],
            },
            handler=_make_fork_handler(fork_callback),
            is_builtin=True,
        ))


# ── Handler factories ─────────────────────────────────────────────────────────

def _make_reflect_handler(sgr_tracker: "SGRTracker") -> Callable:
    """Returns a handler that records SGR and returns 'Reflection noted.'"""
    def handler(**kwargs: Any) -> str:
        # Recording is done in the agent's main loop (needs step number)
        return "Reflection noted."
    return handler


def _make_done_handler(callback: Optional[Callable]) -> Callable:
    """Returns a handler that signals agent to stop."""
    def handler(**kwargs: Any) -> str:
        if callback:
            callback(kwargs)
        return "Review submitted."
    return handler


def _make_spawn_handler(callback: Optional[Callable]) -> Callable:
    """Returns a handler that delegates to the runner's spawn logic."""
    def handler(**kwargs: Any) -> str:
        if callback:
            result = callback(kwargs)
            return json.dumps({"status": "completed", "output": result}, default=str)
        return json.dumps({"status": "spawn not available"})
    return handler


def _make_fork_handler(callback: Optional[Callable]) -> Callable:
    """Returns a handler that delegates to the runner's fork logic."""
    def handler(**kwargs: Any) -> str:
        if callback:
            results = callback(kwargs)
            return json.dumps({"status": "completed", "branches": len(results)}, default=str)
        return json.dumps({"status": "fork not available"})
    return handler
