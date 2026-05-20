"""
Builtin tools: reflect, done, agent_spawn, agent_await, agent_list,
agent_inspect.

Registered automatically based on AgentConfig.tools — the same flat
list every other tool comes from. The framework-built-in handlers
live on the Agent (e.g. _meta_agent_spawn); these registrations just
provide the OpenAI function schemas.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, TYPE_CHECKING

from .registry import ToolDef, ToolRegistry
from ..events import EventType

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
    (agent._meta_agent_spawn, etc.) so they go through registry.dispatch()
    and get schema validation. Otherwise, placeholder handlers are used.

    The effective tool set is the UNION of `agent_config.tools` and any
    `tools` extension declared in the agent's parsed user-message
    frontmatter (`agent._fm_meta`). Without this union we'd silently
    skip registering tools that the user prompt asks for as extensions
    — e.g. reviewer's @tools is now [diff_*, reflect, done] and
    agent_spawn / agent_list live only in reviewer.user.md `tools:`
    block. Without the union the spawned reviewer's child registry
    would never get those builtins and _build_tool_names would raise.
    """

    # Effective tool set = base @tools UNION user-prompt frontmatter
    # `tools:` (always additive in the user layer). Computed once
    # and shared by every builtin below; see register_builtins
    # docstring for the why.
    tool_names = set(agent_config.tools or [])
    fm_meta = getattr(agent, "_fm_meta", None) or {}
    fm_tools = fm_meta.get("tools")
    if isinstance(fm_tools, list):
        tool_names |= {n for n in fm_tools if isinstance(n, str)}

    # ── reflect ───────────────────────────────────────────────────────────
    if "reflect" in tool_names and sgr_tracker:
        schema = sgr_tracker.build_reflect_schema()

        # Handler runs ONLY when registry.dispatch's `_validate_args`
        # accepts the call — schema-invalid arguments short-circuit
        # with a "validation error: ..." string that the model sees as
        # the tool result. Side effects (SGR record, AGENT_REFLECT
        # event, cadence-counter reset) therefore happen only on a
        # well-formed reflect, not on malformed ones. Before this
        # consolidation the agent loop ran sgr.record + counter reset
        # inline BEFORE dispatch, so malformed reflects (e.g.
        # qwen3-6 stuffing JSON into a single `learned` string) were
        # silently treated as successful — driving reflect-spam
        # loops where the model kept retrying because it never saw
        # an error and the cadence pusher's nudges kept firing.
        _captured_sgr = sgr_tracker
        _captured_agent = agent

        def _reflect_handler(**kw):
            # Validation already passed (registry.dispatch's
            # `_validate_args` ran before us). Side effects fire only
            # on well-formed reflects; malformed ones short-circuit
            # at validation and the model sees the error in its
            # tool_result. The cadence counter is owned by
            # `ReflectCadenceCounter` in the pusher pipeline — it
            # observes this step's `step_outcomes` in phase 2 and
            # resets itself on a successful reflect. We don't touch
            # the counter from here.
            if _captured_agent is not None:
                step = getattr(_captured_agent, "_current_step", -1)
                _captured_sgr.record(step, kw)
                if _captured_agent.event_bus is not None:
                    _captured_agent.event_bus.emit(
                        EventType.AGENT_REFLECT,
                        agent_id=_captured_agent.agent_id,
                        agent_name=_captured_agent.config.name,
                        step=step,
                        **kw,
                    )
            # Tool-result string. Default = "Reflection noted."
            # Opt-in via prompt frontmatter (or a mounted skill)
            # `reflect: { with_state: true }` composes a state
            # snapshot (budget + time + subagents) so the agent
            # re-grounds its planning on every reflect — no
            # separate budget_stats query needed.
            template_name = "default"
            children_snapshot = None
            state = None
            if _captured_agent is not None:
                reflect_cfg = getattr(_captured_agent.config, "reflect", None) or {}
                if reflect_cfg.get("with_state"):
                    template_name = "with_state"
                state = _captured_agent.budget_state
                # Snapshot children under the lock so async-spawn
                # races don't tear the rendering.
                if template_name != "default" and state is not None:
                    children_snapshot = []
                    with _captured_agent._children_lock:
                        for cid, child in _captured_agent._children.items():
                            completed = cid in _captured_agent._children_results
                            bs = child.budget_state
                            children_snapshot.append({
                                "name": child.config.name,
                                "focus": (child.data_scope or {}).get("focus", ""),
                                "status": "completed" if completed else "running",
                                "steps_used": bs.steps_used if bs else 0,
                                "tokens_in": bs.tokens_in if bs else 0,
                                "cumulative_paid": bs.cumulative_paid if bs else 0,
                            })
            from ..reflect_response import render as _render_reflect
            return _render_reflect(template_name, state=state,
                                    children=children_snapshot)

        registry.register_tool_def(ToolDef(
            name="reflect",
            description=(
                "Structured self-reflection. Call every 3-5 steps to track progress, "
                "avoid going in circles, and plan the next action."
            ),
            parameters=schema,
            handler=_reflect_handler,
            is_builtin=True,
        ))

    # ── answer ────────────────────────────────────────────────────────────
    # Terminal capture-tool for tasks whose deliverable is a single text
    # payload (abstract reasoning fixtures, /ask flows, anywhere "submit
    # your answer and exit" is the natural close). Mirrors `done` as a
    # special-cased terminator in agent.py's dispatch loop — calling
    # `answer(text=...)` records the text as the run's intended_text AND
    # ends the agent loop. Removes the fragile two-step
    # text_answer-then-done coordination problem (models routinely
    # dropped one of them).
    if "answer" in tool_names:
        registry.register_tool_def(ToolDef(
            name="answer",
            description=(
                "Submit your final text answer AND terminate the run "
                "in a single call. Pass the answer as the `text` "
                "argument. The framework records the text as the "
                "run's deliverable and closes the agent loop — no "
                "separate `done()` call is needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Your final answer as plain text.",
                    },
                },
                "required": ["text"],
            },
            handler=lambda **kw: "Answer submitted.",
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

    if "agent_spawn" in tool_names:
        registry.register_tool_def(ToolDef(
            name="agent_spawn",
            description=(
                "Spawn a sub-agent on a focused task. sched='sync' "
                "(default) runs it to completion and returns the "
                "result. sched='async' returns a run_id immediately — "
                "retrieve the result later with agent_await, or watch "
                "it with agent_inspect. Multiple agent_spawn calls in "
                "the same step run in parallel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name from the registry (use agent_list() to see what's available).",
                    },
                    "focus": {
                        "type": "string",
                        "description": "What this sub-agent should investigate / do.",
                    },
                    "sched": {
                        "type": "string",
                        "enum": ["sync", "async"],
                        "description": "'sync' (default) blocks for the result; 'async' returns a run_id immediately.",
                    },
                    "callback": {
                        "type": "boolean",
                        "description": "async only. true (default): the child's result is auto-delivered into your next step when it finishes — no agent_await needed. false: you must agent_await to collect it.",
                    },
                },
                "required": ["agent", "focus"],
            },
            handler=_meta_handler("_meta_agent_spawn"),
            is_builtin=True,
        ))

    if "agent_await" in tool_names:
        registry.register_tool_def(ToolDef(
            name="agent_await",
            description=(
                "Wait for async-spawned sub-agents to finish. Omit "
                "run_id to wait for ALL of your uncollected async "
                "runs; pass a run_id to wait for one. Returns a "
                "result discriminated by status: 'completed' (all "
                "done), 'partial' (timeout — some still running), "
                "'interrupted' (a budget threshold is about to fire — "
                "handle that first)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Run id from an async agent_spawn. Omit to wait for all your async runs.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max seconds to wait. Default 60.",
                    },
                },
            },
            handler=_meta_handler("_meta_agent_await"),
            is_builtin=True,
        ))

    if "agent_list" in tool_names:
        registry.register_tool_def(ToolDef(
            name="agent_list",
            description=(
                "The catalog of agent TYPES you can spawn — names, "
                "summaries, tools, and required input-data schemas. "
                "Static registry; answers 'who can I call'. (To watch "
                "the runs you've already spawned, use agent_inspect.)"
            ),
            parameters={"type": "object", "properties": {}},
            handler=_meta_handler("_meta_agent_list"),
            is_builtin=True,
        ))

    if "agent_inspect" in tool_names:
        registry.register_tool_def(ToolDef(
            name="agent_inspect",
            description=(
                "Observation of the agent RUNS you have spawned — "
                "their live state, steps, tokens. Call with no run_id "
                "to list your spawned runs; call with a run_id to "
                "inspect one — view='summary' for state/steps/tokens, "
                "'trace' for the full event tree, 'tokens' for the "
                "token breakdown. (To see which agent TYPES exist, "
                "use agent_list.)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Run id to inspect (from an agent_spawn result). Omit to list spawned runs + the agent registry.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["summary", "trace", "tokens"],
                        "description": "Detail level when a run_id is given. Default 'summary'.",
                    },
                },
            },
            handler=_meta_handler("_meta_agent_inspect"),
            is_builtin=True,
        ))

    if "budget_stats" in tool_names:
        registry.register_tool_def(ToolDef(
            name="budget_stats",
            description=(
                "Report your current budget consumption + a rough "
                "cost-of-spawn estimate. Use when planning whether "
                "to spawn an investigator (offload to a fresh window) "
                "or dig directly (keeps shared-pool spend low). The "
                "output splits into your own LLM session (context, "
                "per-agent) and the pool shared with any children "
                "you spawn (tokens + steps)."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_meta_handler("_meta_budget_stats"),
            is_builtin=True,
        ))
