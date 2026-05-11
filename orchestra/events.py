"""
Event system for orchestra.
Simplified: agent lifecycle + param changes + signals.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

OnEvent = Callable[..., None]


class EventType(Enum):
    # Agent lifecycle
    AGENT_STARTED = "agent_started"
    AGENT_STEP = "agent_step"
    AGENT_REFLECT = "agent_reflect"
    AGENT_DONE = "agent_done"
    AGENT_FORCED_DONE = "agent_forced_done"
    AGENT_SPAWNED = "agent_spawned"
    # Streaming
    AGENT_STREAM = "agent_stream"
    AGENT_TOOL_RESULT = "agent_tool_result"
    # Tool API boundary (full payloads, for tracing)
    AGENT_TOOL_REQUEST = "agent_tool_request"
    AGENT_TOOL_RESPONSE = "agent_tool_response"
    # LLM calls (full prompts and responses for tracing)
    AGENT_LLM_REQUEST = "agent_llm_request"
    AGENT_LLM_RESPONSE = "agent_llm_response"
    # Emitted from the agent's LLM exception handler so the events
    # table — and therefore /qa/sessions — has a terminal marker.
    # Without it a dangling agent_llm_request is the LAST entry the
    # UI sees, and the agent looks "still loading" even though the
    # OTel span around the LLM call already recorded the failure.
    AGENT_LLM_ERROR = "agent_llm_error"
    # Params
    PARAM_ADJUSTED = "param_adjusted"
    # Budget
    BUDGET_THRESHOLD_HIT = "budget_threshold_hit"
    # Signals
    STUCK_DETECTED = "stuck_detected"
    # Condensation
    CONDENSATION_TRIGGERED = "condensation_triggered"
    # Free-form artifact dump (agent or tool drops arbitrary JSON for inspection)
    AGENT_ARTIFACT = "agent_artifact"


class EventBus:
    """Simple synchronous event bus with optional passthrough."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = {}
        self._passthrough: Optional[OnEvent] = None

    def subscribe(self, event_type: EventType | str, handler: Callable) -> None:
        key = event_type.value if isinstance(event_type, EventType) else event_type
        self._subscribers.setdefault(key, []).append(handler)

    def unsubscribe(self, event_type: EventType | str, handler: Callable) -> None:
        key = event_type.value if isinstance(event_type, EventType) else event_type
        handlers = self._subscribers.get(key, [])
        if handler in handlers:
            handlers.remove(handler)

    def set_passthrough(self, on_event: Optional[OnEvent]) -> None:
        self._passthrough = on_event

    def emit(self, event_type: EventType | str, **kwargs: Any) -> None:
        key = event_type.value if isinstance(event_type, EventType) else event_type
        if self._passthrough:
            try:
                self._passthrough(key, **kwargs)
            except Exception:
                log.debug("passthrough handler error for %s", key, exc_info=True)
        for handler in self._subscribers.get(key, []):
            try:
                handler(**kwargs)
            except Exception:
                log.debug("subscriber error for %s", key, exc_info=True)
