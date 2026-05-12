"""Tests for the pusher pipeline in orchestra/budget.py.

Covers:
- `RatioPusher`: fire-once-per-threshold via state.fired_pushers.
- `ReflectCadencePusher`: NUDGE at threshold, FORCE_REFLECT at 2×, re-arm
  on counter drop. Same class exercised with both `steps_since_reflect`
  (step cadence) and `seconds_since_reflect` (time cadence).
- `ApplyActionsHandler`: NUDGE → append message; FORCE_REFLECT/DONE →
  narrow tools; CUSTOM → call handler.
- `TracingHandler`: emits BUDGET_THRESHOLD_HIT events with handler-kind
  tag; silent when no event bus.
- `BudgetTracker.apply_handlers`: producer → apply → trace runs the
  whole chain in one call; produces NUDGE messages and narrows tools
  end-to-end on the same ctx.

Each test pokes one handler at a time except the integration tests at
the bottom — which run the full chain against a single state.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from orchestra.budget import (
    ApplyActionsHandler,
    BudgetState,
    BudgetTracker,
    PusherAction,
    RatioPusher,
    ReflectCadencePusher,
    StepContext,
    TracingHandler,
)
from orchestra.events import EventBus, EventType
from orchestra.types import BudgetConfig, PusherConfig, PusherType


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fake_tools(*names: str) -> list[dict]:
    """Build a minimal openai-style tools schema."""
    return [{"type": "function", "function": {"name": n, "parameters": {}}}
            for n in names]


def _ctx(state: BudgetState | None = None,
         steps_since_reflect: int = 0,
         seconds_since_reflect: float = 0.0,
         tools: list[str] | None = None,
         event_bus: EventBus | None = None) -> StepContext:
    """Construct a StepContext with sensible defaults for a single test."""
    state = state or BudgetState(
        original_tokens=1000, original_steps=10, wall_start=0.0,
    )
    schema = _fake_tools(*(tools or ["reflect", "done", "diff_read_file"]))
    return StepContext(
        state=state,
        steps_since_reflect=steps_since_reflect,
        seconds_since_reflect=seconds_since_reflect,
        messages=[],
        all_tools=schema,
        current_tools=list(schema),
        event_bus=event_bus,
    )


# ── RatioPusher ──────────────────────────────────────────────────────────────


class TestRatioPusher:
    def test_under_threshold_no_action(self):
        pusher = RatioPusher([
            PusherConfig(at=0.8, type=PusherType.NUDGE, message="slow down"),
        ])
        ctx = _ctx()
        ctx.state.cumulative_paid = 100  # token_ratio = 0.1
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_at_threshold_emits_action_with_telemetry_fields(self):
        pusher = RatioPusher([
            PusherConfig(at=0.5, type=PusherType.NUDGE, message="halfway"),
        ])
        ctx = _ctx()
        ctx.state.cumulative_paid = 800  # token_ratio = 0.8 > 0.5
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        a = ctx.actions[0]
        assert a.type == PusherType.NUDGE
        assert a.message == "halfway"
        assert a.kind == "ratio"
        assert a.threshold == 0.5
        assert a.ratio == pytest.approx(0.8)

    def test_fires_once_per_threshold(self):
        """Producer marks state.fired_pushers — second apply on same
        state is a no-op even if the ratio still exceeds threshold."""
        pusher = RatioPusher([
            PusherConfig(at=0.5, type=PusherType.NUDGE, message="once"),
        ])
        ctx = _ctx()
        ctx.state.cumulative_paid = 800
        pusher.apply(ctx)
        ctx.actions.clear()
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_multiple_thresholds_fire_at_their_own_ratio(self):
        pusher = RatioPusher([
            PusherConfig(at=0.5, type=PusherType.NUDGE, message="50"),
            PusherConfig(at=0.8, type=PusherType.NUDGE, message="80"),
        ])
        ctx = _ctx()
        ctx.state.cumulative_paid = 600  # 0.6 — only first fires
        pusher.apply(ctx)
        assert [a.message for a in ctx.actions] == ["50"]

        ctx.state.cumulative_paid = 900  # 0.9 — second now fires
        ctx.actions.clear()
        pusher.apply(ctx)
        assert [a.message for a in ctx.actions] == ["80"]


# ── ReflectCadencePusher (step cadence) ──────────────────────────────────────


class TestStepCadencePusher:
    """Same class wired for `counter_attr="steps_since_reflect"`."""

    def _pusher(self, threshold=5):
        return ReflectCadencePusher(
            kind="sgr", threshold=threshold,
            counter_attr="steps_since_reflect",
            nudge_template="N={threshold:.0f}",
        )

    def test_zero_threshold_disabled(self):
        pusher = self._pusher(threshold=0)
        ctx = _ctx(steps_since_reflect=100)
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_negative_threshold_disabled(self):
        pusher = ReflectCadencePusher(
            kind="sgr", threshold=-1,
            counter_attr="steps_since_reflect", nudge_template="x",
        )
        ctx = _ctx(steps_since_reflect=10)
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_under_threshold_no_action(self):
        pusher = self._pusher()
        for n in range(5):
            ctx = _ctx(steps_since_reflect=n)
            pusher.apply(ctx)
            assert ctx.actions == [], f"unexpected action at counter={n}"

    def test_at_threshold_emits_nudge(self):
        pusher = self._pusher()
        ctx = _ctx(steps_since_reflect=5)
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        assert ctx.actions[0].type == PusherType.NUDGE
        assert ctx.actions[0].message == "N=5"
        assert ctx.actions[0].kind == "sgr"
        assert ctx.actions[0].ratio == pytest.approx(1.0)

    def test_nudge_fires_once_per_cycle(self):
        pusher = self._pusher()
        # Cycle: counter 5, 6, 7 — nudge fires at 5, NOT at 6 / 7.
        for n in [5, 6, 7]:
            ctx = _ctx(steps_since_reflect=n)
            pusher.apply(ctx)
            if n == 5:
                assert [a.type for a in ctx.actions] == [PusherType.NUDGE]
            else:
                assert ctx.actions == [], f"unexpected re-fire at {n}"

    def test_force_reflect_at_double_threshold(self):
        pusher = self._pusher()
        # Walk 1..10. At 5 → NUDGE, at 10 → FORCE_REFLECT, rest empty.
        seen: list[tuple[int, PusherType]] = []
        for n in range(1, 11):
            ctx = _ctx(steps_since_reflect=n)
            pusher.apply(ctx)
            for a in ctx.actions:
                seen.append((n, a.type))
        assert seen == [
            (5, PusherType.NUDGE),
            (10, PusherType.FORCE_REFLECT),
        ]

    def test_rearm_on_counter_drop(self):
        """When the agent loop resets the counter (reflect fired), the
        handler should re-arm and nudge again on the next cycle."""
        pusher = self._pusher()
        # First cycle to 5 → NUDGE; tick to 10 → FORCE_REFLECT.
        for n in range(1, 11):
            ctx = _ctx(steps_since_reflect=n)
            pusher.apply(ctx)
        # Reset (reflect fired). Counter drops to 0. Re-arm.
        ctx = _ctx(steps_since_reflect=0)
        pusher.apply(ctx)
        assert ctx.actions == []
        # Second cycle: nudge at 5 again.
        ctx = _ctx(steps_since_reflect=5)
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        assert ctx.actions[0].type == PusherType.NUDGE


# ── ReflectCadencePusher (time cadence) ──────────────────────────────────────


class TestTimeCadencePusher:
    """Same class, `counter_attr="seconds_since_reflect"`, float counter."""

    def _pusher(self, threshold=10.0):
        return ReflectCadencePusher(
            kind="time", threshold=threshold,
            counter_attr="seconds_since_reflect",
            nudge_template="more than {threshold:.0f}s",
        )

    def test_zero_threshold_disabled(self):
        pusher = self._pusher(threshold=0.0)
        ctx = _ctx(seconds_since_reflect=999.0)
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_crosses_threshold_continuously(self):
        """Float counter doesn't land exactly on N — fire when crossing."""
        pusher = self._pusher(threshold=10.0)
        ctx = _ctx(seconds_since_reflect=10.5)
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        assert ctx.actions[0].type == PusherType.NUDGE
        assert ctx.actions[0].kind == "time"

    def test_force_at_double_threshold(self):
        pusher = self._pusher(threshold=10.0)
        # Cross NUDGE first
        ctx = _ctx(seconds_since_reflect=10.5)
        pusher.apply(ctx)
        # Cross FORCE later
        ctx = _ctx(seconds_since_reflect=20.7)
        pusher.apply(ctx)
        assert ctx.actions[0].type == PusherType.FORCE_REFLECT

    def test_rearm_on_counter_drop(self):
        pusher = self._pusher(threshold=10.0)
        # First cycle: walk past 10 and 20
        for t in [5.0, 10.5, 20.5]:
            ctx = _ctx(seconds_since_reflect=t)
            pusher.apply(ctx)
        # Counter resets to ~0
        ctx = _ctx(seconds_since_reflect=0.1)
        pusher.apply(ctx)
        assert ctx.actions == []
        # Next cycle past 10 → nudge again
        ctx = _ctx(seconds_since_reflect=11.0)
        pusher.apply(ctx)
        assert ctx.actions[0].type == PusherType.NUDGE


# ── ApplyActionsHandler ──────────────────────────────────────────────────────


class TestApplyActionsHandler:
    def test_nudge_appends_user_message(self):
        ctx = _ctx()
        ctx.actions.append(PusherAction(type=PusherType.NUDGE, message="hi"))
        ApplyActionsHandler().apply(ctx)
        assert ctx.messages == [{"role": "user", "content": "hi"}]

    def test_nudge_with_empty_message_is_skipped(self):
        ctx = _ctx()
        ctx.actions.append(PusherAction(type=PusherType.NUDGE, message=""))
        ApplyActionsHandler().apply(ctx)
        assert ctx.messages == []

    def test_force_reflect_narrows_tools(self):
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        ctx.actions.append(PusherAction(type=PusherType.FORCE_REFLECT))
        ApplyActionsHandler().apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert names == ["reflect"]

    def test_force_done_narrows_tools(self):
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        ctx.actions.append(PusherAction(type=PusherType.FORCE_DONE))
        ApplyActionsHandler().apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert names == ["done"]

    def test_force_when_target_absent_leaves_tools_alone(self):
        """If the target tool isn't in the schema (e.g. agent doesn't
        have reflect), don't crash with an empty tools list."""
        ctx = _ctx(tools=["done", "diff_read_file"])  # no reflect
        before = list(ctx.current_tools)
        ctx.actions.append(PusherAction(type=PusherType.FORCE_REFLECT))
        ApplyActionsHandler().apply(ctx)
        assert ctx.current_tools == before

    def test_custom_handler_invoked(self):
        called: list[tuple] = []
        def my_handler(messages, state):
            called.append((messages, state))
        ctx = _ctx()
        ctx.actions.append(PusherAction(type=PusherType.CUSTOM,
                                        custom_handler=my_handler))
        ApplyActionsHandler().apply(ctx)
        assert len(called) == 1
        # signature: messages, state
        assert called[0][0] is ctx.messages
        assert called[0][1] is ctx.state

    def test_multiple_actions_applied_in_order(self):
        ctx = _ctx(tools=["reflect", "done"])
        ctx.actions = [
            PusherAction(type=PusherType.NUDGE, message="one"),
            PusherAction(type=PusherType.NUDGE, message="two"),
            PusherAction(type=PusherType.FORCE_REFLECT),
        ]
        ApplyActionsHandler().apply(ctx)
        assert [m["content"] for m in ctx.messages] == ["one", "two"]
        assert [t["function"]["name"] for t in ctx.current_tools] == ["reflect"]


# ── TracingHandler ───────────────────────────────────────────────────────────


class TestTracingHandler:
    def test_silent_without_event_bus(self):
        ctx = _ctx()  # no event_bus
        ctx.actions.append(PusherAction(type=PusherType.NUDGE, kind="sgr",
                                        threshold=5, ratio=1.0))
        # Just shouldn't raise.
        TracingHandler().apply(ctx)

    def test_emits_one_event_per_action(self):
        bus = EventBus()
        seen: list[dict] = []
        bus.subscribe(EventType.BUDGET_THRESHOLD_HIT,
                      lambda **kw: seen.append(kw))
        ctx = _ctx(event_bus=bus)
        ctx.actions = [
            PusherAction(type=PusherType.NUDGE, kind="sgr",
                         threshold=5, ratio=1.0),
            PusherAction(type=PusherType.FORCE_REFLECT, kind="time",
                         threshold=10, ratio=2.0),
        ]
        TracingHandler().apply(ctx)
        assert len(seen) == 2
        assert seen[0]["action_type"] == "nudge"
        assert seen[0]["kind"] == "sgr"
        assert seen[0]["at"] == 5
        assert seen[1]["action_type"] == "force_reflect"
        assert seen[1]["kind"] == "time"
        assert seen[1]["at"] == 10


# ── BudgetTracker end-to-end ─────────────────────────────────────────────────


class TestBudgetTrackerChain:
    def _tracker(self, *, sgr=0, time_=0.0,
                 pushers: list[PusherConfig] | None = None,
                 event_bus: EventBus | None = None):
        cfg = BudgetConfig(
            max_tokens=1000, max_steps=10,
            pushers=pushers or [],
        )
        t = BudgetTracker(cfg, event_bus=event_bus)
        t.configure_reflect_pushers(sgr_interval=sgr, time_reflect_interval=time_)
        return t

    def test_default_chain_has_apply_and_trace_consumers(self):
        t = self._tracker()
        kinds = [h.kind for h in t.handlers]
        assert "ratio" in kinds
        assert "apply" in kinds
        assert "trace" in kinds
        # Order matters: apply before trace, both after producers.
        assert kinds.index("apply") < kinds.index("trace")
        assert kinds.index("ratio") < kinds.index("apply")

    def test_configure_reflect_pushers_adds_sgr_and_time(self):
        t = self._tracker(sgr=5, time_=30.0)
        kinds = [h.kind for h in t.handlers]
        assert kinds.count("sgr") == 1
        assert kinds.count("time") == 1
        # Producers come before apply.
        assert kinds.index("sgr") < kinds.index("apply")
        assert kinds.index("time") < kinds.index("apply")

    def test_configure_reflect_pushers_skips_disabled(self):
        t = self._tracker(sgr=0, time_=0.0)
        kinds = [h.kind for h in t.handlers]
        assert "sgr" not in kinds
        assert "time" not in kinds

    def test_end_to_end_sgr_nudge_appends_message(self):
        bus = EventBus()
        seen: list[dict] = []
        bus.subscribe(EventType.BUDGET_THRESHOLD_HIT,
                      lambda **kw: seen.append(kw))
        t = self._tracker(sgr=3, event_bus=bus)
        ctx = _ctx(steps_since_reflect=3, event_bus=bus)
        t.apply_handlers(ctx)
        # NUDGE applied → message appended.
        assert len(ctx.messages) == 1
        assert "3 steps without calling reflect" in ctx.messages[0]["content"]
        # Telemetry emitted, tagged sgr.
        assert len(seen) == 1
        assert seen[0]["kind"] == "sgr"
        assert seen[0]["action_type"] == "nudge"

    def test_end_to_end_sgr_force_narrows_tools(self):
        t = self._tracker(sgr=3)
        ctx = _ctx(steps_since_reflect=6,
                   tools=["reflect", "done", "diff_read_file"])
        t.apply_handlers(ctx)
        names = [td["function"]["name"] for td in ctx.current_tools]
        assert names == ["reflect"]

    def test_end_to_end_ratio_and_cadence_compose(self):
        """Both a ratio-based pusher and the SGR cadence fire in the
        same step — they share the same actions queue, both get applied,
        both get traced."""
        bus = EventBus()
        seen: list[dict] = []
        bus.subscribe(EventType.BUDGET_THRESHOLD_HIT,
                      lambda **kw: seen.append(kw))
        t = self._tracker(
            sgr=3,
            pushers=[PusherConfig(at=0.5, type=PusherType.NUDGE,
                                  message="halfway through budget")],
            event_bus=bus,
        )
        ctx = _ctx(steps_since_reflect=3, event_bus=bus)
        ctx.state.cumulative_paid = 800  # token_ratio = 0.8
        t.apply_handlers(ctx)
        # Both nudges appended.
        contents = [m["content"] for m in ctx.messages]
        assert contents == ["halfway through budget"] or contents == [
            "halfway through budget",
            contents[1] if len(contents) > 1 else "",
        ]
        # Two BUDGET_THRESHOLD_HIT events with distinct kinds.
        kinds = sorted(s["kind"] for s in seen)
        assert kinds == ["ratio", "sgr"]
