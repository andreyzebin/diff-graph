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
  - `RatioPusher`           : `BudgetConfig.pushers` — one-shot per
                              threshold against `state.max_ratio`.
  - `TimeBudgetPusher`      : NUDGE → FORCE_REFLECT → FORCE_DONE at
                              configured fractions of `max_wall_time`.
                              No-op when no wall budget is set.
  - `ReflectCadencePusher`  : counter + threshold → NUDGE then escalate
                              (kind="reflect")  to FORCE_REFLECT, re-arms each reflect
                              cycle. Step-count cadence by default; the
                              class is generic so a custom user could
                              wire it to any monotonic counter.

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

    Lifecycle (single ctx, two phases):

    1. **Pre-LLM** — agent loop builds ctx, calls
       `tracker.apply_handlers(ctx)`. Producers append actions,
       consumers translate them into `messages` / `current_tools`
       mutations + telemetry. Agent then calls the LLM and dispatches
       tools.

    2. **Post-dispatch** — agent loop populates `step_outcomes` (the
       names + is_error flags of every tool call this step ran) and
       calls `tracker.notify_step_done(ctx)`. Stateful counters
       (e.g. `ReflectCadenceCounter`) update their internal state
       here, ready to surface a fresh value in the next step's
       phase-1 apply.

    `step_outcomes` is empty in phase 1 and populated in phase 2;
    handlers that only react to phase 1 can ignore it.
    """
    state: BudgetState
    # Counters resolved by handlers in phase 1 apply (e.g. the
    # ReflectCadenceCounter writes its tally here so cadence pushers
    # downstream can read it).
    steps_since_reflect: int = 0
    seconds_since_reflect: float = 0.0
    # Mutable IO — handlers append to messages and narrow current_tools.
    messages: list[dict] = field(default_factory=list)
    all_tools: list[dict] = field(default_factory=list)
    current_tools: list[dict] = field(default_factory=list)
    # Producer output / consumer input.
    actions: list[PusherAction] = field(default_factory=list)
    # Phase 2 input — populated by the agent loop after dispatch.
    # Each entry is `(tool_name, is_error)` where is_error is True
    # for a registry.dispatch validation failure (handler did not
    # run). Counter / cadence handlers read this in on_step_done.
    step_outcomes: list[tuple[str, bool]] = field(default_factory=list)
    # Telemetry attribution.
    event_bus: Optional[EventBus] = None
    agent_id: str = ""
    agent_name: str = ""


# ── Handler interface ────────────────────────────────────────────────────────


class PusherHandler(Protocol):
    """One pipeline stage. Reads / mutates `ctx`, returns None.

    Two optional phases per step:

    - `apply(ctx)`         — pre-LLM. Runs in pipeline order. Producers
                             write to `ctx.actions`, consumers translate
                             them into `messages` / `current_tools`
                             mutations + telemetry.
    - `on_step_done(ctx)`  — post-dispatch. Stateful handlers (e.g. the
                             reflect-cadence counter) inspect
                             `ctx.step_outcomes` and update their
                             internal state for the next step. Default
                             no-op (handlers that don't need it just
                             omit the method).
    """
    kind: str

    def apply(self, ctx: StepContext) -> None: ...


# ── Producers ────────────────────────────────────────────────────────────────


class ReflectCadenceCounter:
    """Stateful counter for "tool calls since the last successful
    reflect". Owns the value; the agent loop never tracks it
    directly.

    Phase 1 (`apply`): write the current counter into
    `ctx.steps_since_reflect` so the cadence pusher and any other
    downstream reader see a consistent snapshot.

    Phase 2 (`on_step_done`): inspect `ctx.step_outcomes`. A reflect
    call that ran successfully (handler executed, no schema validation
    error) resets the counter to zero before adding any other tools
    from the same step. A reflect call that failed validation is
    silently ignored — it doesn't pretend to reset the counter, so
    the next cadence threshold still fires and the model sees the
    "validation error: …" string in its tool_result.
    """
    kind = "counter"

    def __init__(self) -> None:
        self._counter: int = 0

    def apply(self, ctx: StepContext) -> None:
        ctx.steps_since_reflect = self._counter

    def on_step_done(self, ctx: StepContext) -> None:
        # `done` is the terminal action — counter is irrelevant after
        # it. Other non-reflect tools each move the model one step
        # further from its last reflect.
        non_reflect = sum(
            1 for name, _err in ctx.step_outcomes
            if name not in ("reflect", "done")
        )
        reflected_ok = any(
            name == "reflect" and not err
            for name, err in ctx.step_outcomes
        )
        if reflected_ok:
            self._counter = non_reflect
        else:
            self._counter += non_reflect


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


class RatioEscalationPusher:
    """One-axis budget escalation: NUDGE → FORCE_REFLECT → FORCE_DONE
    at configurable fractions of one BudgetState ratio attribute.

    Reads `state.<ratio_attr>` (e.g. `token_ratio` or `wall_ratio`).
    The attribute must return a 0..1 float or None (None = the
    dimension isn't configured → handler is a no-op). Each threshold
    latches once per run; ratios only climb so we never re-arm.

    Subclassed below as `TokenBudgetPusher` and `TimeBudgetPusher`
    with axis-appropriate default messages.
    """
    kind: str = ""
    ratio_attr: str = ""

    DEFAULT_NUDGE_AT: float = 0.5
    DEFAULT_FORCE_REFLECT_AT: float = 0.75
    DEFAULT_FORCE_DONE_AT: float = 1.0

    # Subclass overrides — what gets put in front of the model when
    # this dimension's threshold trips.
    DEFAULT_NUDGE_MSG: str = ""
    DEFAULT_FORCE_DONE_MSG: str = ""

    def __init__(
        self,
        *,
        nudge_at: Optional[float] = None,
        force_reflect_at: Optional[float] = None,
        force_done_at: Optional[float] = None,
        nudge_message: Optional[str] = None,
        force_done_message: Optional[str] = None,
    ) -> None:
        # Sorted by ascending ratio so escalation always fires in order
        # even if a misconfigured instance lists FORCE_DONE before
        # NUDGE.
        self._levels: list[tuple[float, PusherType, str]] = sorted([
            (nudge_at if nudge_at is not None else self.DEFAULT_NUDGE_AT,
             PusherType.NUDGE,
             nudge_message if nudge_message is not None else self.DEFAULT_NUDGE_MSG),
            (force_reflect_at if force_reflect_at is not None else self.DEFAULT_FORCE_REFLECT_AT,
             PusherType.FORCE_REFLECT, ""),
            (force_done_at if force_done_at is not None else self.DEFAULT_FORCE_DONE_AT,
             PusherType.FORCE_DONE,
             force_done_message if force_done_message is not None else self.DEFAULT_FORCE_DONE_MSG),
        ], key=lambda x: x[0])
        self._fired: list[bool] = [False] * len(self._levels)

    def apply(self, ctx: StepContext) -> None:
        ratio = getattr(ctx.state, self.ratio_attr, None)
        if ratio is None:
            return
        for idx, (at, ptype, msg) in enumerate(self._levels):
            if self._fired[idx]:
                continue
            if ratio < at:
                continue
            self._fired[idx] = True
            ctx.actions.append(PusherAction(
                type=ptype,
                message=msg,
                kind=self.kind,
                threshold=at,
                ratio=ratio,
            ))


class TokenBudgetPusher(RatioEscalationPusher):
    """Token-spend escalation on `state.token_ratio`
    (= cumulative_paid / max_tokens, capped at 1.0). Always-on; the
    ratio is 0 when no tokens have been spent so the handler stays
    silent on cheap runs.

    Messages are deliberately generic — no role-specific wording
    ("post findings", "consolidate review") and no tool-specific
    wording ("call done()"). The FORCE_DONE action narrows the
    tool surface for us; the message only needs to motivate
    "wrap up now". Agent-specific wrap-up phrasing lives in the
    prompt.
    """
    kind = "token-budget"
    ratio_attr = "token_ratio"

    DEFAULT_NUDGE_MSG = (
        "Half of your token budget is used. Plan what's left so you "
        "finish before it runs out."
    )
    DEFAULT_FORCE_DONE_MSG = (
        "Token budget exhausted. Submit your final output now."
    )


class TimeBudgetPusher(RatioEscalationPusher):
    """Wall-clock escalation on `state.wall_ratio`. No-op if
    `max_wall_time` is unset (wall_ratio returns None)."""
    kind = "time-budget"
    ratio_attr = "wall_ratio"

    DEFAULT_NUDGE_MSG = (
        "Half of your wall-clock budget is used. Plan what's left "
        "so you finish before it runs out."
    )
    DEFAULT_FORCE_DONE_MSG = (
        "Wall-clock budget exhausted. Submit your final output now."
    )


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
        #
        # Default producers (in pipeline order):
        # - ReflectCadenceCounter — owns `steps_since_reflect`. Apply
        #                           writes the counter into ctx; the
        #                           counter itself is mutated in
        #                           `on_step_done`. Must come first so
        #                           downstream cadence pushers see a
        #                           fresh value.
        # - RatioPusher           — user-configurable thresholds on
        #                           `state.max_ratio`. Empty by
        #                           default (per-dimension escalation
        #                           is handled by the dedicated
        #                           pushers below).
        # - TokenBudgetPusher     — always-on, NUDGE 0.5 →
        #                           FORCE_REFLECT 0.75 → FORCE_DONE
        #                           1.0 on `state.token_ratio`.
        # - TimeBudgetPusher      — same shape on `state.wall_ratio`,
        #                           no-op without `max_wall_time`.
        # Step budget is intentionally NOT a separate escalation axis
        # — `token_ratio` covers the same "agent is overrunning"
        # signal in practice (each step costs roughly bounded
        # tokens), and `max_steps` still acts as the hard cap via
        # `state.exhausted` in the agent loop.
        self._producers: list[PusherHandler] = [
            ReflectCadenceCounter(),
            RatioPusher(config.pushers),
            TokenBudgetPusher(),
            TimeBudgetPusher(),
        ]
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

    def configure_reflect_pushers(self, reflect_interval: int = 0) -> None:
        """Append the step-cadence reflect producer to the chain.
        Idempotent in the sense that the agent calls it once during
        __init__ after parsing config. `reflect_interval <= 0` is a
        no-op.

        Wall-clock reflect pressure is handled by the always-on
        `TimeBudgetPusher` (NUDGE/FORCE_REFLECT/FORCE_DONE at fractions
        of max_wall_time) — a fixed-interval cadence on wall clock
        proved redundant with that escalation.
        """
        if reflect_interval > 0:
            self.add_producer(ReflectCadencePusher(
                kind="reflect",
                threshold=float(reflect_interval),
                counter_attr="steps_since_reflect",
                nudge_template=(
                    "You've taken {threshold:.0f} steps without calling reflect(). "
                    "Pause and call reflect(confidence=…, learned=…, "
                    "questions_remaining=[…]) — consolidate what you've learned, "
                    "what's still open, and what's resolved. Then continue."
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
        """Phase 1: pre-LLM. Walk producers → consumers, each handler
        may mutate `ctx` (append actions, narrow current_tools,
        append messages, emit events)."""
        if ctx.event_bus is None:
            ctx.event_bus = self._event_bus
        for handler in self.handlers:
            handler.apply(ctx)

    def notify_step_done(self, ctx: StepContext) -> None:
        """Phase 2: post-dispatch. Handlers that implement
        `on_step_done` update their internal state based on what tools
        ran this step (see `ctx.step_outcomes`)."""
        for handler in self.handlers:
            hook = getattr(handler, "on_step_done", None)
            if hook is not None:
                hook(ctx)

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
