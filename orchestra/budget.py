"""
Budget tracking, pusher evaluation, and child budget partitioning.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import BudgetConfig, PusherConfig, PusherType
from .events import EventBus, EventType

log = logging.getLogger(__name__)


@dataclass
class PusherAction:
    """Resolved action from a budget pusher."""
    type: PusherType
    message: str = ""
    handler: Optional[Callable] = None


@dataclass
class BudgetState:
    """Mutable runtime state for one agent's budget."""
    tokens_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    steps_used: int = 0
    wall_start: float = 0.0

    original_tokens: int = 40_000
    original_steps: int = 40
    original_wall_time: Optional[float] = None

    fired_pushers: set[int] = field(default_factory=set)  # indices already fired

    @property
    def token_ratio(self) -> float:
        if self.original_tokens <= 0:
            return 0.0
        return min(self.tokens_used / self.original_tokens, 1.0)

    @property
    def step_ratio(self) -> float:
        if self.original_steps <= 0:
            return 0.0
        return min(self.steps_used / self.original_steps, 1.0)

    @property
    def wall_ratio(self) -> Optional[float]:
        if self.original_wall_time is None or self.original_wall_time <= 0:
            return None
        elapsed = time.time() - self.wall_start
        return min(elapsed / self.original_wall_time, 1.0)

    @property
    def max_ratio(self) -> float:
        """Highest of all budget dimensions."""
        ratios = [self.token_ratio, self.step_ratio]
        wr = self.wall_ratio
        if wr is not None:
            ratios.append(wr)
        return max(ratios)

    @property
    def exhausted(self) -> bool:
        return self.max_ratio >= 1.0

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.original_tokens - self.tokens_used)

    @property
    def steps_remaining(self) -> int:
        return max(0, self.original_steps - self.steps_used)


class BudgetTracker:
    """Manages budget lifecycle for one agent."""

    def __init__(self, config: BudgetConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self._event_bus = event_bus

    def start(self) -> BudgetState:
        """Create a fresh budget state and start the wall clock."""
        return BudgetState(
            wall_start=time.time(),
            original_tokens=self.config.max_tokens,
            original_steps=self.config.max_steps,
            original_wall_time=self.config.max_wall_time,
        )

    def record_step(self, state: BudgetState, tokens_in: int = 0,
                    tokens_out: int = 0, tokens_cached: int = 0) -> None:
        """Record one ReAct step's token usage."""
        state.steps_used += 1
        state.tokens_in = tokens_in
        state.tokens_out = tokens_out
        state.tokens_cached = tokens_cached
        state.tokens_used = tokens_in + tokens_out  # total for this step pair

    def update_tokens(self, state: BudgetState, total_tokens: int,
                      tokens_in: int = 0, tokens_out: int = 0,
                      tokens_cached: int = 0) -> None:
        """Update cumulative token usage from LLM response."""
        state.tokens_used = total_tokens
        state.tokens_in = tokens_in
        state.tokens_out = tokens_out
        state.tokens_cached = tokens_cached

    def check_pushers(self, state: BudgetState) -> list[PusherAction]:
        """Evaluate pushers and return newly-triggered actions."""
        actions: list[PusherAction] = []
        ratio = state.max_ratio

        for idx, pusher in enumerate(self.config.pushers):
            if idx in state.fired_pushers:
                continue
            if ratio >= pusher.at:
                state.fired_pushers.add(idx)
                action = PusherAction(
                    type=pusher.type,
                    message=pusher.message,
                    handler=_resolve_handler(pusher.handler) if pusher.handler else None,
                )
                actions.append(action)

                if self._event_bus:
                    self._event_bus.emit(
                        EventType.BUDGET_THRESHOLD_HIT,
                        at=pusher.at,
                        ratio=ratio,
                        action_type=pusher.type.value,
                    )
        return actions

    def allocate_child(self, parent: BudgetState, fraction: float = 0.5) -> BudgetConfig:
        """Create a child budget config from parent's remaining budget."""
        max_frac = self.config.max_children_budget
        effective = min(fraction, max_frac)

        child_tokens = int(parent.tokens_remaining * effective)
        child_steps = max(1, int(parent.steps_remaining * effective))
        child_wall: Optional[float] = None
        if parent.original_wall_time is not None:
            elapsed = time.time() - parent.wall_start
            remaining = max(0, parent.original_wall_time - elapsed)
            child_wall = remaining * effective

        return BudgetConfig(
            max_tokens=child_tokens,
            max_steps=child_steps,
            max_wall_time=child_wall,
            max_children_budget=max_frac,
            pushers=list(self.config.pushers),  # inherit pusher config
        )

    def debit_child(self, parent: BudgetState, child: BudgetState) -> None:
        """Debit parent by child's actual consumption."""
        parent.tokens_used += child.tokens_used
        # steps are not debited — parent's step count is its own loop iterations


def _resolve_handler(dotted_path: str) -> Optional[Callable]:
    """Import a dotted path like 'mymod.func' and return the callable."""
    try:
        parts = dotted_path.rsplit(".", 1)
        if len(parts) == 2:
            import importlib
            mod = importlib.import_module(parts[0])
            return getattr(mod, parts[1])
    except Exception:
        log.warning("could not resolve handler: %s", dotted_path)
    return None
