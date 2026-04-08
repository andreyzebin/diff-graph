"""
Builtin tools: reflect, done, plan, spawn_agent, fork, create_topology.

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
    plan_callback: Optional[Callable[[dict], Any]] = None,
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

    # ── plan ──────────────────────────────────────────────────────────────
    if "plan" in (agent_config.spawn_tools or []):
        registry.register_tool_def(ToolDef(
            name="plan",
            description=(
                "Spawn a planner sub-agent to create a structured plan for a goal. "
                "The planner analyzes the goal and constraints, then returns a JSON plan "
                "with prioritized tasks. Use this when you need to break down a complex "
                "problem before investigating, or when you want a second opinion on strategy."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What you need a plan for.",
                    },
                    "constraints": {
                        "type": "string",
                        "description": "Constraints, context, or what you already know.",
                    },
                    "output_hint": {
                        "type": "string",
                        "description": "Optional: what shape the plan should take (e.g. 'list of tasks', 'decision tree').",
                    },
                },
                "required": ["goal"],
            },
            handler=_make_plan_handler(plan_callback),
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


def _make_plan_handler(callback: Optional[Callable]) -> Callable:
    """Returns a handler that spawns a planner sub-agent."""
    def handler(**kwargs: Any) -> str:
        if callback:
            result = callback(kwargs)
            if isinstance(result, dict):
                return json.dumps(result, indent=2, ensure_ascii=False, default=str)
            return str(result)
        return json.dumps({"status": "plan not available"})
    return handler


# ── Default plan agent prompt ─────────────────────────────────────────────────

DEFAULT_PLAN_PROMPT = """You are a planning agent. Given a goal and optional constraints, \
produce a structured JSON plan.

OUTPUT FORMAT — return ONLY valid JSON:
{
  "analysis": "<1-2 sentence assessment of what's needed>",
  "tasks": [
    {
      "id": "<short_snake_case>",
      "priority": "high|medium|low",
      "focus": "<what specifically to do>",
      "rationale": "<why this task matters>"
    }
  ],
  "risks": ["<potential issue>"],
  "recommendation": "<which task to start with and why>"
}

RULES:
- 2-5 tasks, ordered by priority.
- Be specific: name actual things to investigate, not generic statements.
- If constraints mention what's already known, don't duplicate that work.
"""

DEFAULT_SGR_SPAWN_PROMPT = """You are a focused research agent. You have been given a specific \
question to investigate. Use the available tools to find the answer.

When done, call done() with your findings. Be thorough but focused — \
answer the specific question, don't expand scope.
"""
