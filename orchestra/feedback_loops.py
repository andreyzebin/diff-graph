"""
Cross-agent feedback loops.

Declared at topology level, connecting an observer to target agents.
When a trigger fires and the condition is met, actions are executed
on the target agent (param adjust, message inject, budget change, etc.).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

from .types import FeedbackLoopConfig, PusherType
from .events import EventBus, EventType

if TYPE_CHECKING:
    from .agent import Agent

log = logging.getLogger(__name__)


class FeedbackLoop:
    """Single feedback loop: observer watches targets, fires actions on conditions."""

    def __init__(self, config: FeedbackLoopConfig, event_bus: EventBus) -> None:
        self.config = config
        self._event_bus = event_bus
        self._targets: dict[str, "Agent"] = {}  # agent_id -> Agent

    def register_target(self, agent_id: str, agent: "Agent") -> None:
        self._targets[agent_id] = agent

    def attach(self) -> None:
        """Subscribe to relevant events based on trigger type."""
        trigger = self.config.trigger

        event_map = {
            "on_child_reflect": EventType.AGENT_REFLECT,
            "on_child_step": EventType.AGENT_STEP,
            "on_child_stuck": EventType.STUCK_DETECTED,
            "on_child_done": EventType.AGENT_DONE,
        }

        event_type = event_map.get(trigger)
        if event_type:
            self._event_bus.subscribe(event_type, self._on_event)

    def _on_event(self, **kwargs: Any) -> None:
        """Event handler: check condition and fire actions."""
        agent_id = kwargs.get("agent_id", "")

        # Check if this agent is one of our targets
        agent = self._targets.get(agent_id)
        if agent is None:
            return

        # Build state dict for condition evaluation
        state = dict(kwargs)
        if agent.sgr and agent.sgr.last:
            state["sgr_confidence"] = agent.sgr.confidence_numeric
            state["sgr_question_count"] = agent.sgr.open_question_count
            state["sgr_staleness"] = agent.sgr.staleness

        if agent.budget_state:
            state["budget_ratio"] = agent.budget_state.max_ratio
            state["step_ratio"] = agent.budget_state.step_ratio

        # Evaluate condition
        if self.config.condition:
            if not _eval_simple_condition(self.config.condition, state):
                return

        # Execute actions
        for action in self.config.actions:
            self._execute_action(action, agent, state)

        self._event_bus.emit(EventType.FEEDBACK_LOOP_FIRED,
                            observer=self.config.observer,
                            target_agent_id=agent_id,
                            actions=len(self.config.actions))

    def _execute_action(self, action: dict, agent: "Agent", state: dict) -> None:
        """Execute one feedback loop action on the target agent."""
        action_type = action.get("type", "")

        if action_type == "adjust_param":
            param = action.get("param", "")
            value = action.get("value")
            if param and value is not None:
                # Apply to agent's base LLM params
                if hasattr(agent, '_base_llm_params'):
                    agent._base_llm_params[param] = value
                    self._event_bus.emit(EventType.PARAM_ADJUSTED,
                                        agent_id=agent.agent_id,
                                        param=param, value=value,
                                        source="feedback_loop")

        elif action_type == "inject_message":
            message = action.get("message", "")
            if message:
                # This is tricky — we'd need access to the agent's message list
                # For now, log it. The agent's main loop can check a feedback queue.
                log.info("feedback loop inject_message for %s: %s",
                         agent.agent_id, message[:100])

        elif action_type == "extend_budget":
            steps = action.get("steps", 0)
            tokens = action.get("tokens", 0)
            max_delta = self.config.max_feedback_budget_delta
            if agent.budget_state:
                if steps and (max_delta is None or steps <= max_delta):
                    agent.budget_state.original_steps += steps
                if tokens and (max_delta is None or tokens <= max_delta):
                    agent.budget_state.original_tokens += tokens

        elif action_type == "reduce_budget":
            steps = action.get("steps", 0)
            if agent.budget_state and steps:
                agent.budget_state.original_steps = max(
                    agent.budget_state.steps_used + 1,
                    agent.budget_state.original_steps - steps,
                )

        elif action_type == "force_reflect":
            log.info("feedback loop force_reflect for %s", agent.agent_id)
            # Agent's main loop will check this via a flag

        elif action_type == "force_done":
            log.info("feedback loop force_done for %s", agent.agent_id)

        else:
            log.warning("unknown feedback loop action type: %s", action_type)


class FeedbackLoopManager:
    """Manages all feedback loops for a topology execution."""

    def __init__(
        self,
        configs: list[FeedbackLoopConfig],
        event_bus: EventBus,
    ) -> None:
        self._loops = [FeedbackLoop(c, event_bus) for c in configs]
        self._event_bus = event_bus

    def register_agent(self, agent_name: str, agent_id: str, agent: "Agent") -> None:
        """Register an agent as a potential target for feedback loops."""
        for loop in self._loops:
            if agent_name in loop.config.targets or agent_id in loop.config.targets:
                loop.register_target(agent_id, agent)

    def attach_all(self) -> None:
        """Subscribe all loops to their events."""
        for loop in self._loops:
            loop.attach()


def _eval_simple_condition(condition: str, state: dict) -> bool:
    """Evaluate simple conditions like 'sgr_confidence == low AND step_ratio > 0.5'."""
    # Split on AND
    parts = [p.strip() for p in condition.split("AND")]
    for part in parts:
        if not _eval_single(part, state):
            return False
    return True


def _eval_single(expr: str, state: dict) -> bool:
    """Evaluate a single comparison expression."""
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in expr:
            left, right = expr.split(op, 1)
            left = left.strip()
            right = right.strip()

            # Resolve left side from state
            left_val = state.get(left, left)

            # Try numeric
            try:
                lv = float(left_val)
                rv = float(right)
                if op == "==": return lv == rv
                if op == "!=": return lv != rv
                if op == ">=": return lv >= rv
                if op == "<=": return lv <= rv
                if op == ">": return lv > rv
                if op == "<": return lv < rv
            except (ValueError, TypeError):
                pass

            # String comparison
            ls = str(left_val).strip('"\'').lower()
            rs = right.strip('"\'').lower()
            if op == "==": return ls == rs
            if op == "!=": return ls != rs
            break

    return False
