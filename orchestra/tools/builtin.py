"""
Builtin tools: reflect, done, spawn_agent, list_agents.

Registered automatically based on AgentConfig.tools — the same flat
list every other tool comes from. The framework-built-in handlers
live on the Agent (e.g. _meta_spawn_agent); these registrations just
provide the OpenAI function schemas.
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
    agent: Any = None,
) -> None:
    """Register builtin tools based on agent config.

    If agent is provided, meta-tools are registered with real handlers
    (agent._meta_spawn_agent, etc.) so they go through registry.dispatch()
    and get schema validation. Otherwise, placeholder handlers are used.

    The effective tool set is the UNION of `agent_config.tools` and any
    `tools_add` extension declared in the agent's parsed user-message
    frontmatter (`agent._fm_meta`). Without this union we'd silently
    skip registering tools that the user prompt asks for as extensions
    — e.g. reviewer's @tools is now [diff_*, reflect, done] and
    spawn_agent / list_agents live only in reviewer.user.md tools_add.
    Without the union the spawned reviewer's child registry would
    never get those builtins and _build_tool_names would raise.
    """

    # Effective tool set = base @tools UNION user-prompt frontmatter
    # `tools_add`. Computed once and shared by every builtin below;
    # see register_builtins docstring for the why.
    tool_names = set(agent_config.tools or [])
    fm_meta = getattr(agent, "_fm_meta", None) or {}
    fm_tools_add = fm_meta.get("tools_add")
    if isinstance(fm_tools_add, list):
        tool_names |= {n for n in fm_tools_add if isinstance(n, str)}

    # ── reflect ───────────────────────────────────────────────────────────
    if "reflect" in tool_names and sgr_tracker:
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
    if "done" in tool_names:
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

    # ── Framework tools (schemas only — Agent handles execution) ──────────

    def _meta_handler(method_name: str) -> Callable:
        """Return agent's meta-method as handler, or placeholder if no agent.

        Meta-methods take a single `args` dict, but dispatch() calls
        handler(**kwargs). This wrapper bridges the two conventions.
        """
        if agent and hasattr(agent, method_name):
            method = getattr(agent, method_name)
            return lambda **kw: method(kw)
        return lambda **kw: "handled by agent"

    if "spawn_agent" in tool_names:
        registry.register_tool_def(ToolDef(
            name="spawn_agent",
            description=(
                "Spawn a sub-agent on a focused task. The sub-agent runs to "
                "completion and its result is returned. Multiple spawn_agent "
                "calls in the same step run in parallel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name from the registry (use list_agents() to see what's available).",
                    },
                    "focus": {
                        "type": "string",
                        "description": "What this sub-agent should investigate / do.",
                    },
                },
                "required": ["agent", "focus"],
            },
            handler=_meta_handler("_meta_spawn_agent"),
            is_builtin=True,
        ))

    if "list_agents" in tool_names:
        registry.register_tool_def(ToolDef(
            name="list_agents",
            description=(
                "Get the registry of all available agents: names, summaries, "
                "and required input data schemas. Use this to discover which "
                "agent to spawn for a given task."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_meta_handler("_meta_list_agents"),
            is_builtin=True,
        ))
