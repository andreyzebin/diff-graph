"""
Builtin tools: reflect, done, plan, spawn_agent, spawn_many,
fork, adjust_agent, observe_agents.

Registered automatically based on agent config (sgr, meta_tools).
Meta-tools are handled directly by Agent — these registrations
just provide the OpenAI function schemas.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, TYPE_CHECKING

from .registry import ToolDef, ToolRegistry

if TYPE_CHECKING:
    from ..sgr import SGRTracker


def register_builtins(
    registry: ToolRegistry,
    agent_config: Any,
    sgr_tracker: Optional["SGRTracker"] = None,
) -> None:
    """Register builtin tools based on agent config."""

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
            handler=lambda **kw: "Reflection noted.",
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
        handler=lambda **kw: "Output submitted.",
        is_builtin=True,
    ))

    # ── Meta-tools (schemas only — Agent handles execution) ───────────────
    meta = set(agent_config.meta_tools or [])

    if "spawn_agent" in meta:
        registry.register_tool_def(ToolDef(
            name="spawn_agent",
            description=(
                "Spawn a sub-agent. Use list_agents() first to see available agents and "
                "their required input data. Pass data fields matching the agent's @data schema — "
                "they are injected into the agent's prompt {placeholders}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name from the registry."},
                    "data": {"type": "object", "description": "Input data matching the agent's @data schema. Injected into prompt {placeholders}."},
                    "focus": {"type": "string", "description": "Additional focus instruction (appended to context)."},
                    "context_handoff": {
                        "type": "string",
                        "description": "Context to pass: sgr_outcomes, full_history, findings_only, condensed.",
                        "enum": ["sgr_outcomes", "full_history", "findings_only", "condensed"],
                    },
                    "wait": {"type": "boolean", "description": "Wait for completion (default true)."},
                },
                "required": ["agent"],
            },
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "spawn_many" in meta:
        registry.register_tool_def(ToolDef(
            name="spawn_many",
            description=(
                "Spawn multiple agents in parallel and return merged results. "
                "Use for fan-out investigation across multiple angles."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string"},
                                "focus": {"type": "string"},
                            },
                            "required": ["agent", "focus"],
                        },
                    },
                    "context_handoff": {"type": "string", "enum": ["sgr_outcomes", "findings_only"]},
                    "merge": {"type": "string", "enum": ["union", "best_confidence", "llm_merge", "raw"]},
                },
                "required": ["agents"],
            },
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "plan" in meta:
        registry.register_tool_def(ToolDef(
            name="plan",
            description=(
                "Create a structured plan for a goal. Returns JSON with tasks, "
                "priorities, risks, and recommendations. Use when you need to "
                "break down a complex problem or get a strategic perspective."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "What you need a plan for."},
                    "constraints": {"type": "string", "description": "Context, constraints, what you already know."},
                    "output_hint": {"type": "string", "description": "Desired plan format."},
                },
                "required": ["goal"],
            },
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "fork" in meta:
        registry.register_tool_def(ToolDef(
            name="fork",
            description=(
                "Fork into parallel branches, each exploring a different hypothesis. "
                "Results are merged and returned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "branches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "focus": {"type": "string", "description": "What this branch investigates."},
                            },
                            "required": ["focus"],
                        },
                    },
                    "context_handoff": {"type": "string", "enum": ["full_history", "sgr_outcomes"]},
                    "merge": {"type": "string", "enum": ["best_confidence", "union", "llm_merge"]},
                },
                "required": ["branches"],
            },
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "adjust_agent" in meta:
        registry.register_tool_def(ToolDef(
            name="adjust_agent",
            description=(
                "Adjust a child agent's LLM parameters, inject a message, or extend its budget. "
                "Use to steer child agents: raise temperature for creativity, add penalties "
                "to break loops, inject context they're missing, or give more budget."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "ID of the child agent to adjust."},
                    "temperature": {"type": "number", "description": "New temperature (0-2)."},
                    "frequency_penalty": {"type": "number", "description": "New frequency penalty (-2 to 2)."},
                    "presence_penalty": {"type": "number", "description": "New presence penalty (-2 to 2)."},
                    "top_p": {"type": "number", "description": "New top_p (0-1)."},
                    "max_completion_tokens": {"type": "integer"},
                    "model": {"type": "string", "description": "Switch to a different model."},
                    "inject_message": {"type": "string", "description": "Message to inject into agent's context."},
                    "extend_budget_steps": {"type": "integer", "description": "Extra steps to grant."},
                },
                "required": ["agent_id"],
            },
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "observe_agents" in meta:
        registry.register_tool_def(ToolDef(
            name="observe_agents",
            description=(
                "Get the current status of all child agents: step count, budget usage, "
                "SGR state (confidence, open questions, learned), last tool called."
            ),
            parameters={"type": "object", "properties": {}},
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))

    if "list_agents" in meta:
        registry.register_tool_def(ToolDef(
            name="list_agents",
            description=(
                "Get the registry of all available agents: names, summaries, capabilities, "
                "required input data schemas. Use this to discover which agent to spawn "
                "for a given task."
            ),
            parameters={"type": "object", "properties": {}},
            handler=lambda **kw: "handled by agent",
            is_builtin=True,
        ))


# ── Default prompts ───────────────────────────────────────────────────────────

DEFAULT_PLAN_PROMPT = """You are a planning agent. Given a goal and optional constraints, \
produce a structured JSON plan.

OUTPUT FORMAT — return ONLY valid JSON:
{
  "analysis": "<1-2 sentence assessment>",
  "tasks": [
    {
      "id": "<short_snake_case>",
      "priority": "high|medium|low",
      "focus": "<what specifically to do>",
      "rationale": "<why this matters>"
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
