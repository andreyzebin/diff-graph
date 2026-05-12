"""
Budget tracking, pusher pipeline, child budget partitioning.

## Pusher pipeline (middleware chain)

Each step the agent loop builds a `StepContext` and hands it to
`BudgetTracker.apply_handlers(ctx)`. The tracker walks an ordered chain
of `PusherHandler`s — each handler may inspect or mutate the context.

The chain has two layers:

- **Producers** — read `ctx.state` and the cadence counters, append
  `PusherAction`s to `ctx.actions`. Stateful (cycle latches), pure
  with respect to messages / tools.
  - `RatioPusher`            : `BudgetConfig.pushers` (one-shot/threshold)
  - `ReflectCadencePusher(kind="sgr")`  : step-count reflect cadence
  - `ReflectCadencePusher(kind="time")` : wall-clock reflect cadence

- **Consumers** — always last in the chain, in this order:
  - `ApplyActionsHandler`   : translate each pending action into a
                              mutation on `ctx.messages` /
                              `ctx.current_tools`.
  - `TracingHandler`        : emit a `BUDGET_THRESHOLD_HIT` event per
                              applied action, tagged by handler `kind`.

That separation means:
- Adding a new pusher dimension = one new producer subclass, plug it
  into the chain. No change to apply or tracing.
- Changing how an action mutates the conversation = edit ApplyActions
  only.
- Changing telemetry shape = edit TracingHandler only.

`StepContext` is the single mutable state passed through the chain.
`PusherAction` is the wire format between producers and consumers.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .types import BudgetConfig, PusherConfig, PusherType
from .events import EventBus, EventType

log = logging.getLogger(__name__)


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
    cache_discount: float = 0.1

    # Cumulative paid = sum of per-step deltas
    cumulative_paid: int = 0
    _prev_step_paid: int = 0  # last step's paid for delta computation

    fired_pushers: set[int] = field(default_factory=set)

    @property
    def tokens_paid(self) -> int:
        """Cumulative effective tokens paid (sum of per-step deltas)."""
        return self.cumulative_paid

    def _compute_step_paid(self) -> int:
        """Per-call paid cost for the current step."""
        uncached_in = max(0, self.tokens_in - self.tokens_cached)
        cached_cost = int(self.tokens_cached * self.cache_discount)
        return uncached_in + cached_cost + self.tokens_out

    @property
    def token_ratio(self) -> float:
        if self.original_tokens <= 0:
            return 0.0
        return min(self.cumulative_paid / self.original_tokens, 1.0)

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
        return max(0, self.original_tokens - self.tokens_paid)

    @property
    def steps_remaining(self) -> int:
        return max(0, self.original_steps - self.steps_used)


# ── Wire format between producers and consumers ──────────────────────────────


@dataclass
class PusherAction:
    """Producer-side description of "something needs to happen this step".

    Consumers translate this into mutations + telemetry. Producers
    never touch `ctx.messages` or `ctx.current_tools` directly — they
    just append actions, the apply handler does the work.
    """
    type: PusherType
    message: str = ""
    custom_handler: Optional[Callable] = None
    kind: str = ""             # tag of the producing handler (ratio / sgr / time / …)
    threshold: float = 0.0     # value the counter crossed; telemetry
    ratio: float = 0.0         # counter / threshold; telemetry


@dataclass
class StepContext:
    """Per-step middleware context.

    The pipeline writes to `actions` (producers), then to `messages` /
    `current_tools` (apply), then emits events (tracing). The agent
    loop reads `current_tools` and the (mutated) `messages` for the
    next LLM call.
    """
    state: BudgetState
    # Counters reset by the agent loop when reflect actually fires.
    steps_since_reflect: int = 0
    seconds_since_reflect: float = 0.0
    # Mutable IO — handlers append to messages and narrow current_tools.
    messages: list[dict] = field(default_factory=list)
    all_tools: list[dict] = field(default_factory=list)
    current_tools: list[dict] = field(default_factory=list)
    # Producer output / consumer input.
    actions: list[PusherAction] = field(default_factory=list)
    # Telemetry attribution.
    event_bus: Optional[EventBus] = None
    agent_id: str = ""
    agent_name: str = ""


# ── Handler interface ────────────────────────────────────────────────────────


class PusherHandler(Protocol):
    """One pipeline stage. Reads / mutates `ctx`, returns None."""
    kind: str

    def apply(self, ctx: StepContext) -> None: ...


# ── Producers ────────────────────────────────────────────────────────────────


class RatioPusher:
    """Budget-ratio one-shot producers from `BudgetConfig.pushers`.

    Each configured threshold fires exactly once per run (tracked via
    `state.fired_pushers`). On fire: append a `PusherAction` carrying
    the configured type/message. Apply + telemetry happen downstream.
    """
    kind = "ratio"

    def __init__(self, pushers: list[PusherConfig]) -> None:
        self._pushers = pushers

    def apply(self, ctx: StepContext) -> None:
        ratio = ctx.state.max_ratio
        for idx, pusher in enumerate(self._pushers):
            if idx in ctx.state.fired_pushers:
                continue
            if ratio < pusher.at:
                continue
            ctx.state.fired_pushers.add(idx)
            ctx.actions.append(PusherAction(
                type=pusher.type,
                message=pusher.message,
                custom_handler=_resolve_handler(pusher.handler) if pusher.handler else None,
                kind=self.kind,
                threshold=pusher.at,
                ratio=ratio,
            ))


class ReflectCadencePusher:
    """Generic reflect-cadence producer: counter + threshold → NUDGE,
    escalate to FORCE_REFLECT, re-armed every reflect cycle.

    Cycle = monotonic stretch of the counter between two reflect calls.
    The agent loop resets the counter to 0 when reflect fires; this
    handler detects the reset as a counter drop and re-arms its latches.

    Same class drives step-cadence (`counter_attr="steps_since_reflect"`)
    and time-cadence (`counter_attr="seconds_since_reflect"`).
    """

    def __init__(
        self,
        *,
        kind: str,
        threshold: float,
        counter_attr: str,
        nudge_template: str,
    ) -> None:
        self.kind = kind
        self.threshold = threshold
        self._counter_attr = counter_attr
        self._nudge_template = nudge_template

        # Per-cycle latches: both reset on counter drop.
        self._nudged_in_cycle = False
        self._forced_in_cycle = False
        self._last_counter: float = 0.0

    def apply(self, ctx: StepContext) -> None:
        if self.threshold <= 0:
            return
        counter: float = getattr(ctx, self._counter_attr)
        # Strict `<` so a flat tick (no progress) keeps the latches latched.
        if counter < self._last_counter:
            self._nudged_in_cycle = False
            self._forced_in_cycle = False
        self._last_counter = counter

        if not self._forced_in_cycle and counter >= 2 * self.threshold:
            self._forced_in_cycle = True
            ctx.actions.append(PusherAction(
                type=PusherType.FORCE_REFLECT,
                kind=self.kind,
                threshold=self.threshold,
                ratio=counter / self.threshold,
            ))
        elif not self._nudged_in_cycle and counter >= self.threshold:
            self._nudged_in_cycle = True
            ctx.actions.append(PusherAction(
                type=PusherType.NUDGE,
                message=self._nudge_template.format(threshold=self.threshold),
                kind=self.kind,
                threshold=self.threshold,
                ratio=counter / self.threshold,
            ))


# ── Consumers ────────────────────────────────────────────────────────────────


class ApplyActionsHandler:
    """Translate each pending action into a mutation on `ctx`.

    Action → mutation table:
      NUDGE          → append a user message to ctx.messages
      FORCE_REFLECT  → narrow ctx.current_tools to just `reflect`
      FORCE_DONE     → narrow ctx.current_tools to just `done`
      CUSTOM         → call the action's custom handler with (messages, state)

    Sole writer to `ctx.messages` and `ctx.current_tools`. If a future
    pusher needs a new mutation shape, extend this table — every
    producer benefits without per-producer wiring.
    """
    kind = "apply"

    def apply(self, ctx: StepContext) -> None:
        for action in ctx.actions:
            t = action.type
            if t == PusherType.NUDGE:
                if action.message:
                    ctx.messages.append({"role": "user", "content": action.message})
            elif t == PusherType.FORCE_REFLECT:
                _narrow_to(ctx, "reflect")
            elif t == PusherType.FORCE_DONE:
                _narrow_to(ctx, "done")
            elif t == PusherType.CUSTOM and action.custom_handler:
                try:
                    action.custom_handler(ctx.messages, ctx.state)
                except Exception as exc:  # noqa: BLE001 — defensive
                    log.warning("custom pusher failed: %s", exc)


class TracingHandler:
    """Emit one `BUDGET_THRESHOLD_HIT` event per pending action,
    tagged with the producing handler's `kind`. Cheap to skip when
    `event_bus is None` (unit tests run without one)."""
    kind = "trace"

    def apply(self, ctx: StepContext) -> None:
        bus = ctx.event_bus
        if bus is None:
            return
        for action in ctx.actions:
            bus.emit(
                EventType.BUDGET_THRESHOLD_HIT,
                agent_id=ctx.agent_id,
                agent_name=ctx.agent_name,
                at=action.threshold,
                ratio=action.ratio,
                action_type=action.type.value,
                kind=action.kind,
            )


def _narrow_to(ctx: StepContext, tool_name: str) -> None:
    """Filter current_tools to just `tool_name`. If absent, leave alone
    — better to let the agent keep going than crash on empty schema."""
    narrowed = [
        t for t in ctx.all_tools
        if t.get("function", {}).get("name") == tool_name
    ]
    if narrowed:
        ctx.current_tools = narrowed


# ── Tracker ──────────────────────────────────────────────────────────────────


class BudgetTracker:
    """Manages budget lifecycle for one agent + walks the pusher chain."""

    def __init__(
        self,
        config: BudgetConfig,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config
        self._event_bus = event_bus
        # Two-tier chain so adding producers via configure_reflect_pushers
        # always slots them BEFORE apply + trace.
        self._producers: list[PusherHandler] = [RatioPusher(config.pushers)]
        self._consumers: list[PusherHandler] = [
            ApplyActionsHandler(),
            TracingHandler(),
        ]

    @property
    def event_bus(self) -> Optional[EventBus]:
        return self._event_bus

    @property
    def handlers(self) -> list[PusherHandler]:
        """Full chain in execution order: producers then consumers."""
        return list(self._producers) + list(self._consumers)

    def add_producer(self, handler: PusherHandler) -> None:
        """Slot a new producer before the consumers."""
        self._producers.append(handler)

    def configure_reflect_pushers(
        self,
        sgr_interval: int = 0,
        time_reflect_interval: float = 0.0,
    ) -> None:
        """Append reflect-cadence producers to the chain. Idempotent in
        the sense that the agent calls this once during __init__ after
        parsing config. Zero/negative intervals leave that handler off."""
        if sgr_interval > 0:
            self.add_producer(ReflectCadencePusher(
                kind="sgr",
                threshold=float(sgr_interval),
                counter_attr="steps_since_reflect",
                nudge_template=(
                    "You've taken {threshold:.0f} steps without calling reflect(). "
                    "Pause and call reflect(confidence=…, learned=…, "
                    "questions_remaining=[…]) — consolidate what you've learned, "
                    "what's still open, and what's resolved. Then continue."
                ),
            ))
        if time_reflect_interval > 0:
            self.add_producer(ReflectCadencePusher(
                kind="time",
                threshold=float(time_reflect_interval),
                counter_attr="seconds_since_reflect",
                nudge_template=(
                    "More than {threshold:.0f}s have passed without a reflect() "
                    "call. Pause now and call reflect(confidence=…, learned=…, "
                    "questions_remaining=[…]) — slow tools are burning wall "
                    "time, consolidate before the next move."
                ),
            ))

    def start(self) -> BudgetState:
        """Create a fresh budget state and start the wall clock."""
        return BudgetState(
            wall_start=time.time(),
            original_tokens=self.config.max_tokens,
            original_steps=self.config.max_steps,
            original_wall_time=self.config.max_wall_time,
            cache_discount=self.config.cache_discount,
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
        """Update token usage. Computes per-step delta and accumulates."""
        state.tokens_used = total_tokens
        state.tokens_in = tokens_in
        state.tokens_out = tokens_out
        state.tokens_cached = tokens_cached

        # Compute this step's paid cost and accumulate delta
        current_step_paid = state._compute_step_paid()
        delta = current_step_paid - state._prev_step_paid
        if delta < 0:
            delta = current_step_paid  # first step or reset
        state.cumulative_paid += delta
        state._prev_step_paid = current_step_paid

    def apply_handlers(self, ctx: StepContext) -> None:
        """Walk producers → consumers. Each handler may mutate `ctx`."""
        if ctx.event_bus is None:
            ctx.event_bus = self._event_bus
        for handler in self.handlers:
            handler.apply(ctx)

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
