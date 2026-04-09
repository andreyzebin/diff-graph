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
    # LLM calls (full prompts and responses for tracing)
    AGENT_LLM_REQUEST = "agent_llm_request"
    AGENT_LLM_RESPONSE = "agent_llm_response"
    # Params
    PARAM_ADJUSTED = "param_adjusted"
    # Budget
    BUDGET_THRESHOLD_HIT = "budget_threshold_hit"
    # Signals
    STUCK_DETECTED = "stuck_detected"
    # Condensation
    CONDENSATION_TRIGGERED = "condensation_triggered"


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
