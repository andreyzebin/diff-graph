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
    FailedReflectGuard,
    PusherAction,
    RatioPusher,
    ReflectCadencePusher,
    StepBudgetPusher,
    StepContext,
    TimeBudgetPusher,
    TokenBudgetPusher,
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

    def _pusher(self, threshold=5, force_reflect_multiplier=None):
        return ReflectCadencePusher(
            kind="reflect", threshold=threshold,
            counter_attr="steps_since_reflect",
            nudge_template="N={threshold:.0f}",
            force_reflect_multiplier=force_reflect_multiplier,
        )

    def test_zero_threshold_disabled(self):
        pusher = self._pusher(threshold=0)
        ctx = _ctx(steps_since_reflect=100)
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_negative_threshold_disabled(self):
        pusher = ReflectCadencePusher(
            kind="reflect", threshold=-1,
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
        assert ctx.actions[0].kind == "reflect"
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

    def test_force_reflect_default_disabled(self):
        """Default: FORCE_REFLECT is opt-in
        (`force_reflect_multiplier=None`). Walking the counter past
        2× threshold emits only the NUDGE — no FORCE_REFLECT action.
        Why default-off: narrowing tools to reflect-only dead-ends
        the agent when reflect itself fails validation."""
        pusher = self._pusher()  # no multiplier
        seen: list[tuple[int, PusherType]] = []
        for n in range(1, 11):
            ctx = _ctx(steps_since_reflect=n)
            pusher.apply(ctx)
            for a in ctx.actions:
                seen.append((n, a.type))
        # Only the NUDGE at the threshold — no FORCE_REFLECT.
        assert seen == [(5, PusherType.NUDGE)]

    def test_force_reflect_at_double_threshold_when_opted_in(self):
        """With `force_reflect_multiplier=2`, FORCE_REFLECT fires at
        2× threshold — the pre-refactor default, now opt-in."""
        pusher = self._pusher(force_reflect_multiplier=2)
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
        # Use FORCE_REFLECT opt-in so the "first cycle reaches 10 =
        # 2× threshold" path is exercised fully — re-arm has to clear
        # both latches.
        pusher = self._pusher(force_reflect_multiplier=2)
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


# ── TimeBudgetPusher ─────────────────────────────────────────────────────────


class TestTimeBudgetPusher:
    """Wall-clock budget escalation: NUDGE → FORCE_REFLECT → FORCE_DONE
    at configured fractions of max_wall_time."""

    def _ctx_with_wall(self, wall_used: float, wall_max: float = 100.0) -> StepContext:
        """A StepContext whose wall_ratio is `wall_used / wall_max`."""
        state = BudgetState(
            original_tokens=1_000_000,    # huge → token_ratio ≈ 0
            original_steps=1_000_000,
            original_wall_time=wall_max,
            wall_start=__import__("time").time() - wall_used,  # backdate
        )
        return _ctx(state=state)

    def test_no_wall_budget_is_no_op(self):
        """Without max_wall_time, wall_ratio is None → handler skips."""
        pusher = TimeBudgetPusher()
        state = BudgetState(
            original_tokens=1000, original_steps=10,
            original_wall_time=None,  # ← no wall budget
            wall_start=0.0,
        )
        ctx = _ctx(state=state)
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_under_first_threshold_no_action(self):
        pusher = TimeBudgetPusher()
        ctx = self._ctx_with_wall(wall_used=30, wall_max=100)  # ratio 0.30
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_crosses_nudge_at_50pct(self):
        pusher = TimeBudgetPusher()
        ctx = self._ctx_with_wall(wall_used=55, wall_max=100)  # ratio 0.55
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        a = ctx.actions[0]
        assert a.type == PusherType.NUDGE
        assert a.kind == "time-budget"
        assert a.threshold == pytest.approx(0.5)
        assert a.ratio == pytest.approx(0.55, abs=0.05)

    def test_default_escalation_skips_force_reflect(self):
        """Default escalation is 2-level: NUDGE → FORCE_DONE.
        FORCE_REFLECT is opt-in (see
        `test_escalates_through_three_levels_when_opted_in` below).
        Why default-off: narrowing tools to reflect-only dead-ends
        the agent when reflect itself fails validation."""
        pusher = TimeBudgetPusher()
        seen: list[PusherType] = []
        for wall_used in [30, 60, 80, 105]:
            ctx = self._ctx_with_wall(wall_used=wall_used, wall_max=100)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.5 at wall_used=60
            PusherType.FORCE_DONE,    # crossed 1.0 at wall_used=105
        ]

    def test_escalates_through_three_levels_when_opted_in(self):
        """Opt-in: passing `force_reflect_at=0.75` restores the
        pre-refactor 3-level escalation."""
        pusher = TimeBudgetPusher(force_reflect_at=0.75)
        seen: list[PusherType] = []
        for wall_used in [30, 60, 80, 105]:
            ctx = self._ctx_with_wall(wall_used=wall_used, wall_max=100)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.5 at wall_used=60
            PusherType.FORCE_REFLECT, # crossed 0.75 at wall_used=80
            PusherType.FORCE_DONE,    # crossed 1.0 at wall_used=105
        ]

    def test_no_re_arm(self):
        """Wall time only goes up; latches stay fired forever."""
        pusher = TimeBudgetPusher()
        ctx = self._ctx_with_wall(wall_used=60, wall_max=100)
        pusher.apply(ctx)  # fires NUDGE
        ctx2 = self._ctx_with_wall(wall_used=60, wall_max=100)
        pusher.apply(ctx2)  # same level → silent
        assert ctx2.actions == []

    def test_custom_thresholds(self):
        pusher = TimeBudgetPusher(
            nudge_at=0.3, force_reflect_at=0.6, force_done_at=0.9,
        )
        seen: list[PusherType] = []
        for wall_used in [25, 35, 65, 95]:
            ctx = self._ctx_with_wall(wall_used=wall_used, wall_max=100)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.3 at wall_used=35
            PusherType.FORCE_REFLECT, # crossed 0.6 at wall_used=65
            PusherType.FORCE_DONE,    # crossed 0.9 at wall_used=95
        ]

    def test_levels_sorted_even_if_misconfigured(self):
        """Defensive: if author puts FORCE_DONE before NUDGE in the
        constructor, the handler reorders them so escalation still
        fires in ascending threshold order."""
        pusher = TimeBudgetPusher(
            nudge_at=0.9,            # ← intentionally swapped
            force_reflect_at=0.5,
            force_done_at=0.7,
        )
        seen: list[tuple[float, PusherType]] = []
        for wall_used in [55, 75, 95]:
            ctx = self._ctx_with_wall(wall_used=wall_used, wall_max=100)
            pusher.apply(ctx)
            seen.extend((a.threshold, a.type) for a in ctx.actions)
        # Sorted by threshold ascending: 0.5, 0.7, 0.9
        # At wall_used=55 → ratio 0.55 → only 0.5 threshold fires.
        # At wall_used=75 → ratio 0.75 → only 0.7 threshold fires (0.5 already latched).
        # At wall_used=95 → ratio 0.95 → only 0.9 threshold fires.
        assert seen == [
            (0.5, PusherType.FORCE_REFLECT),
            (0.7, PusherType.FORCE_DONE),
            (0.9, PusherType.NUDGE),
        ]


class TestTokenBudgetPusher:
    """Token-spend escalation on `state.token_ratio`. Same shape as
    TimeBudgetPusher (inherits from RatioEscalationPusher) — these
    tests cover the token-specific instance + the inherited contract
    against a different `state` axis."""

    def _ctx_with_tokens(self, tokens_used: int, tokens_max: int = 1000) -> StepContext:
        """A StepContext whose token_ratio is `tokens_used / tokens_max`."""
        state = BudgetState(
            original_tokens=tokens_max,
            original_steps=1_000_000,    # huge → step_ratio ≈ 0
            cumulative_paid=tokens_used,
            wall_start=0.0,
        )
        return _ctx(state=state)

    def test_default_kind(self):
        assert TokenBudgetPusher.kind == "token-budget"
        assert TokenBudgetPusher.ratio_attr == "token_ratio"

    def test_under_first_threshold_no_action(self):
        pusher = TokenBudgetPusher()
        ctx = self._ctx_with_tokens(tokens_used=300, tokens_max=1000)  # 0.30
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_crosses_nudge_at_50pct(self):
        pusher = TokenBudgetPusher()
        ctx = self._ctx_with_tokens(tokens_used=550, tokens_max=1000)  # 0.55
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        a = ctx.actions[0]
        assert a.type == PusherType.NUDGE
        assert a.kind == "token-budget"
        assert a.threshold == pytest.approx(0.5)
        assert a.ratio == pytest.approx(0.55, abs=0.05)
        # And the message must mention tokens specifically — agents
        # need to know which dimension is pressing, not a generic
        # "budget gone" line.
        assert "token" in a.message.lower()

    def test_default_escalation_skips_force_reflect(self):
        """Default: 2-level NUDGE → FORCE_DONE. FORCE_REFLECT opt-in
        (see `..._when_opted_in` below)."""
        pusher = TokenBudgetPusher()
        seen: list[PusherType] = []
        for used in [300, 600, 800, 1050]:
            ctx = self._ctx_with_tokens(tokens_used=used, tokens_max=1000)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.5 at used=600
            PusherType.FORCE_DONE,    # crossed 1.0 at used=1050 (clamped to 1.0)
        ]

    def test_escalates_through_three_levels_when_opted_in(self):
        pusher = TokenBudgetPusher(force_reflect_at=0.75)
        seen: list[PusherType] = []
        for used in [300, 600, 800, 1050]:
            ctx = self._ctx_with_tokens(tokens_used=used, tokens_max=1000)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.5 at used=600
            PusherType.FORCE_REFLECT, # crossed 0.75 at used=800
            PusherType.FORCE_DONE,    # crossed 1.0 at used=1050 (clamped to 1.0)
        ]

    def test_no_re_arm(self):
        """Token usage only goes up; latches stay fired forever."""
        pusher = TokenBudgetPusher()
        ctx = self._ctx_with_tokens(tokens_used=600, tokens_max=1000)
        pusher.apply(ctx)
        ctx2 = self._ctx_with_tokens(tokens_used=650, tokens_max=1000)
        pusher.apply(ctx2)
        assert ctx2.actions == []  # still under 0.75 → silent

    def test_messages_distinguish_dimensions(self):
        """Token vs time nudges carry different copy so the model
        can tell which budget axis is pressuring it."""
        assert (
            TokenBudgetPusher.DEFAULT_NUDGE_MSG
            != TimeBudgetPusher.DEFAULT_NUDGE_MSG
        )
        assert "token" in TokenBudgetPusher.DEFAULT_NUDGE_MSG.lower()
        assert "wall-clock" in TimeBudgetPusher.DEFAULT_NUDGE_MSG.lower()

    def test_messages_are_tool_agnostic(self):
        """Different agents have different tools — the framework's
        budget pusher messages must not mention any specific tool by
        name. Use generic "submit your final output" / "wrap up"
        phrasing instead of `done()` / `pr_post_comment()` etc.
        Agent-specific wrap-up wording is the prompt's job.

        Pinned tool names that historically leaked into pusher
        messages (and would silently mislead an agent that doesn't
        have that tool):
        """
        agent_tools = (
            "done()", "reflect()", "pr_post_comment", "set_review_status",
            "agent_spawn", "pr_list_threads", "pr_read_thread",
            "text_answer", "diff_read_file", "diff_search",
        )
        for cls in (TokenBudgetPusher, TimeBudgetPusher):
            for label, msg in (
                ("NUDGE", cls.DEFAULT_NUDGE_MSG),
                ("FORCE_DONE", cls.DEFAULT_FORCE_DONE_MSG),
            ):
                msg_lower = msg.lower()
                leaked = [t for t in agent_tools if t.lower() in msg_lower]
                assert not leaked, (
                    f"{cls.__name__}.{label} mentions tool name(s) "
                    f"{leaked}; pusher messages must be tool-agnostic. "
                    f"Got: {msg!r}"
                )


# ── StepBudgetPusher ─────────────────────────────────────────────────────────


class TestStepBudgetPusher:
    """Step-count escalation on `state.step_ratio`. Same shape as
    Token/Time pushers (inherits from RatioEscalationPusher) — these
    tests cover the step-specific instance + the **tightened**
    FORCE_DONE threshold (0.90 vs parent's 1.0) which gives the
    model headroom before the hard step cap kicks in."""

    def _ctx_with_steps(self, steps_used: int, steps_max: int = 100) -> StepContext:
        """A StepContext whose step_ratio is `steps_used / steps_max`.

        Tokens + wall are neutralised so this axis can be exercised
        in isolation — same convention as `TestTokenBudgetPusher`."""
        state = BudgetState(
            original_tokens=1_000_000,   # huge → token_ratio ≈ 0
            original_steps=steps_max,
            steps_used=steps_used,
            wall_start=0.0,
        )
        return _ctx(state=state)

    def test_default_kind(self):
        assert StepBudgetPusher.kind == "step-budget"
        assert StepBudgetPusher.ratio_attr == "step_ratio"

    def test_force_done_threshold_tighter_than_parent(self):
        """The class-level override is load-bearing: at ratio=1.0 the
        agent loop has already terminated via `state.exhausted`, so
        FORCE_DONE@1.0 would never get a chance to act. 0.90 gives
        the model ~10% step-budget headroom to actually emit done()
        after seeing the narrow-to-[done] tool list."""
        assert StepBudgetPusher.DEFAULT_FORCE_DONE_AT == 0.90

    def test_under_first_threshold_no_action(self):
        pusher = StepBudgetPusher()
        ctx = self._ctx_with_steps(steps_used=30, steps_max=100)  # 0.30
        pusher.apply(ctx)
        assert ctx.actions == []

    def test_crosses_nudge_at_50pct(self):
        pusher = StepBudgetPusher()
        ctx = self._ctx_with_steps(steps_used=55, steps_max=100)  # 0.55
        pusher.apply(ctx)
        assert len(ctx.actions) == 1
        a = ctx.actions[0]
        assert a.type == PusherType.NUDGE
        assert a.kind == "step-budget"
        assert a.threshold == pytest.approx(0.5)
        # The nudge must mention steps specifically (not generic
        # "budget gone") so a model that's burning token-light steps
        # gets actionable feedback on the right axis.
        assert "step" in a.message.lower()

    def test_default_escalation_skips_force_reflect(self):
        """Default: NUDGE → FORCE_DONE (FORCE_REFLECT opt-in). The
        critical thing for the step axis is FORCE_DONE at 0.90 firing
        BEFORE the hard cap (1.0) — that's covered here and in
        `test_force_done_fires_before_hard_cap`."""
        pusher = StepBudgetPusher()
        seen: list[PusherType] = []
        for used in [40, 60, 80, 95]:
            ctx = self._ctx_with_steps(steps_used=used, steps_max=100)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.50 at used=60
            PusherType.FORCE_DONE,    # crossed 0.90 at used=95 — BEFORE hard cap
        ]

    def test_escalates_through_three_levels_when_opted_in(self):
        """Opt-in: 3-level escalation, FORCE_DONE still at 0.90."""
        pusher = StepBudgetPusher(force_reflect_at=0.75)
        seen: list[PusherType] = []
        for used in [40, 60, 80, 95]:
            ctx = self._ctx_with_steps(steps_used=used, steps_max=100)
            pusher.apply(ctx)
            seen.extend(a.type for a in ctx.actions)
        assert seen == [
            PusherType.NUDGE,         # crossed 0.50 at used=60
            PusherType.FORCE_REFLECT, # crossed 0.75 at used=80
            PusherType.FORCE_DONE,    # crossed 0.90 at used=95
        ]

    def test_force_done_fires_before_hard_cap(self):
        """The whole point of FORCE_DONE@0.90: at step 90/100 the
        agent still has 10 steps to run, sees a tools list narrowed
        to [done], and can flush findings. Without the override
        (i.e. FORCE_DONE@1.0) the agent loop would terminate via
        state.exhausted on the same tick the pusher fires, leaving
        no room for the LLM to act on it."""
        pusher = StepBudgetPusher()
        # Sweep up to but not past 0.90:
        seen_before = []
        for used in [50, 70, 89]:
            ctx = self._ctx_with_steps(steps_used=used, steps_max=100)
            pusher.apply(ctx)
            seen_before.extend(a.type for a in ctx.actions)
        assert PusherType.FORCE_DONE not in seen_before
        # State is NOT yet exhausted (would block agent loop):
        assert not self._ctx_with_steps(89, 100).state.exhausted
        # Now cross 0.90:
        ctx = self._ctx_with_steps(steps_used=90, steps_max=100)
        pusher.apply(ctx)
        assert any(a.type == PusherType.FORCE_DONE for a in ctx.actions)
        # And critically — state.exhausted is STILL False so the
        # agent loop will still call the LLM with the narrowed tools.
        assert not ctx.state.exhausted

    def test_no_re_arm(self):
        """Step count only goes up; latches stay fired forever."""
        pusher = StepBudgetPusher()
        ctx = self._ctx_with_steps(steps_used=60, steps_max=100)
        pusher.apply(ctx)
        ctx2 = self._ctx_with_steps(steps_used=65, steps_max=100)
        pusher.apply(ctx2)
        assert ctx2.actions == []  # still under 0.75 → silent

    def test_messages_distinguish_dimensions(self):
        """Step vs token vs time nudges carry different copy so the
        model can tell WHICH budget axis is pressuring it. This is
        the load-bearing reason we have three separate pusher
        classes (instead of one `max_ratio` pusher)."""
        msgs = {
            "step":  StepBudgetPusher.DEFAULT_NUDGE_MSG,
            "token": TokenBudgetPusher.DEFAULT_NUDGE_MSG,
            "wall":  TimeBudgetPusher.DEFAULT_NUDGE_MSG,
        }
        # All distinct.
        assert len(set(msgs.values())) == 3
        # Each mentions its own dimension.
        assert "step"  in msgs["step"].lower()
        assert "token" in msgs["token"].lower()
        assert "wall-clock" in msgs["wall"].lower()


# ── Axis independence ────────────────────────────────────────────────────────
#
# Plan 204 INV-U-001 surfaced a load-bearing assumption that wasn't
# tested: "token_ratio covers step_ratio in practice (each step
# costs roughly bounded tokens)". qwen3-6 broke it — 30/30 steps
# exhausted with token_ratio still ~0.4. The token escalator never
# fired FORCE_DONE; the agent got killed by `state.exhausted`
# without emitting findings.
#
# This block exists as a structural regression net: every escalation
# axis must work INDEPENDENTLY, not by relying on another to cover
# it. If you ever consider removing a budget axis from the default
# chain because "another one will catch the same case", read these
# tests first.


class TestAxisIndependence:

    def test_step_exhaust_with_low_token_ratio_emits_force_done(self):
        """Token-cheap, step-heavy pattern (qwen3-6 reproduction).
        Without `StepBudgetPusher` in the chain this assertion
        FAILS — token escalator stays at ~0.4 nudge level and the
        agent dies on `state.exhausted` with no FORCE_DONE."""
        state = BudgetState(
            original_tokens=30_000,
            original_steps=30,
            steps_used=27,           # 90% step budget
            cumulative_paid=12_000,  # 40% token budget
            wall_start=0.0,
        )
        ctx = _ctx(state=state)
        # Walk a fresh BudgetTracker through this ctx — replicates
        # production wiring rather than calling pushers directly.
        from orchestra.budget import BudgetTracker
        from orchestra.types import BudgetConfig
        tracker = BudgetTracker(BudgetConfig(max_tokens=30_000, max_steps=30))
        tracker.apply_handlers(ctx)
        # At least ONE pusher in the default chain must have emitted
        # FORCE_DONE for this ratio profile. With StepBudgetPusher
        # in the chain, it does. Without, the chain stays silent on
        # the most-pressing dimension.
        assert any(a.type == PusherType.FORCE_DONE for a in ctx.actions), (
            "step-budget exhaust must escalate to FORCE_DONE "
            "independently of token-budget — see plan 204 INV-U-001"
        )

    def test_token_exhaust_with_low_step_ratio_emits_force_done(self):
        """Inverse: token-heavy, step-light pattern (e.g. a single
        massive prompt with one LLM call). Token escalator catches
        it even when step ratio is tiny."""
        state = BudgetState(
            original_tokens=10_000,
            original_steps=100,
            steps_used=5,           # 5% step budget
            cumulative_paid=10_000, # 100% token budget
            wall_start=0.0,
        )
        ctx = _ctx(state=state)
        from orchestra.budget import BudgetTracker
        from orchestra.types import BudgetConfig
        tracker = BudgetTracker(BudgetConfig(max_tokens=10_000, max_steps=100))
        tracker.apply_handlers(ctx)
        assert any(a.type == PusherType.FORCE_DONE for a in ctx.actions)

    def test_no_axis_exhausted_no_escalation(self):
        """Sanity counter-test: a healthy run with all ratios well
        under 0.5 emits no escalation actions. The chain's overhead
        is bounded to NUDGE-level signals — won't spam an
        already-on-track agent."""
        state = BudgetState(
            original_tokens=30_000,
            original_steps=30,
            steps_used=3,
            cumulative_paid=2_000,
            wall_start=0.0,
        )
        ctx = _ctx(state=state)
        from orchestra.budget import BudgetTracker
        from orchestra.types import BudgetConfig
        tracker = BudgetTracker(BudgetConfig(max_tokens=30_000, max_steps=30))
        tracker.apply_handlers(ctx)
        # ReflectCadenceCounter may fire NUDGE here on the first
        # tick (no reflect yet) — that's fine. The budget axes
        # specifically must NOT have fired FORCE_DONE.
        budget_kinds = {"token-budget", "time-budget", "step-budget"}
        budget_force_done = [a for a in ctx.actions
                              if a.kind in budget_kinds
                              and a.type == PusherType.FORCE_DONE]
        assert budget_force_done == []


# ── FailedReflectGuard ───────────────────────────────────────────────────────


class TestFailedReflectGuard:
    """N consecutive reflect schema-validation failures → latch:
    hide reflect from current_tools and inject a one-shot user
    message. Repro: trace be9b2084aafb on mediaplanner SBLOOM-143
    where qwen3-6 sent 47 consecutive malformed reflects (only
    `learned` field; validation rejected every one) and burned the
    entire 50-step budget without posting findings."""

    def _ctx(self, tools: list[str] | None = None) -> StepContext:
        return _ctx(tools=tools or ["reflect", "done", "pr_post_comment"])

    def test_under_threshold_does_not_latch(self):
        guard = FailedReflectGuard(threshold=3)
        ctx = self._ctx()
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        # 2 < 3 → not latched yet; reflect still in current_tools.
        guard.apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert "reflect" in names

    def test_threshold_latches(self):
        guard = FailedReflectGuard(threshold=3)
        ctx = self._ctx()
        for _ in range(3):
            ctx.step_outcomes = [("reflect", True)]
            guard.on_step_done(ctx)
        # 3 failures in a row → latched.
        guard.apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert "reflect" not in names
        # And the one-shot nudge message is in messages.
        assert ctx.messages, "expected explanatory nudge after latch"
        assert "schema validation" in ctx.messages[0]["content"]
        assert "hidden" in ctx.messages[0]["content"]

    def test_nudge_is_one_shot(self):
        """Re-applying after the latch must NOT keep appending the
        same nudge — that's its own degenerate spam."""
        guard = FailedReflectGuard(threshold=2)
        ctx = self._ctx()
        for _ in range(2):
            ctx.step_outcomes = [("reflect", True)]
            guard.on_step_done(ctx)
        guard.apply(ctx)
        guard.apply(ctx)
        guard.apply(ctx)
        assert len(ctx.messages) == 1, (
            f"nudge must fire exactly once after latch; got "
            f"{len(ctx.messages)} messages: {ctx.messages!r}"
        )

    def test_successful_reflect_resets_streak(self):
        """A reflect that actually passed validation breaks the
        streak — model recovered, no latch needed."""
        guard = FailedReflectGuard(threshold=3)
        ctx = self._ctx()
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        # 2 failures in a row …
        ctx.step_outcomes = [("reflect", False)]   # … then a success
        guard.on_step_done(ctx)
        # Next two failures should NOT trip the threshold.
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        guard.apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert "reflect" in names, "successful reflect should reset the streak"

    def test_non_reflect_tool_does_not_reset_streak(self):
        """Interleaving a non-reflect tool MUST NOT reset the
        counter — otherwise a model could dodge the latch by
        alternating reflect+pr_post_comment."""
        guard = FailedReflectGuard(threshold=3)
        ctx = self._ctx()
        ctx.step_outcomes = [("reflect", True), ("pr_post_comment", False)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True), ("pr_post_comment", False)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        guard.apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert "reflect" not in names

    def test_latch_persists(self):
        """Once latched, reflect stays hidden for the rest of the
        run regardless of subsequent successful tools."""
        guard = FailedReflectGuard(threshold=2)
        ctx = self._ctx()
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        ctx.step_outcomes = [("reflect", True)]
        guard.on_step_done(ctx)
        # Latched. Then many successful non-reflect calls follow:
        for _ in range(5):
            ctx.step_outcomes = [("pr_post_comment", False)]
            guard.on_step_done(ctx)
        ctx2 = self._ctx()
        guard.apply(ctx2)
        names = [t["function"]["name"] for t in ctx2.current_tools]
        assert "reflect" not in names


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

    def test_force_reflect_is_no_op(self):
        """FORCE_REFLECT no longer narrows tools — the action is kept
        as a telemetry-visible signal (so BUDGET_THRESHOLD_HIT events
        still surface "would have force-reflected"), but the tool
        surface is never reduced to reflect-only. See
        `ApplyActionsHandler` docstring."""
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        before = [t["function"]["name"] for t in ctx.current_tools]
        ctx.actions.append(PusherAction(type=PusherType.FORCE_REFLECT))
        ApplyActionsHandler().apply(ctx)
        after = [t["function"]["name"] for t in ctx.current_tools]
        assert after == before

    def test_force_done_narrows_tools(self):
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        ctx.actions.append(PusherAction(type=PusherType.FORCE_DONE))
        ApplyActionsHandler().apply(ctx)
        names = [t["function"]["name"] for t in ctx.current_tools]
        assert names == ["done"]

    def test_force_done_when_target_absent_leaves_tools_alone(self):
        """If `done` isn't in the schema (unusual configuration),
        don't crash with an empty tools list — FORCE_DONE narrowing
        no-ops in that case rather than wiping the surface."""
        ctx = _ctx(tools=["reflect", "diff_read_file"])  # no done
        before = list(ctx.current_tools)
        ctx.actions.append(PusherAction(type=PusherType.FORCE_DONE))
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
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        ctx.actions = [
            PusherAction(type=PusherType.NUDGE, message="one"),
            PusherAction(type=PusherType.NUDGE, message="two"),
            PusherAction(type=PusherType.FORCE_DONE),
        ]
        ApplyActionsHandler().apply(ctx)
        assert [m["content"] for m in ctx.messages] == ["one", "two"]
        assert [t["function"]["name"] for t in ctx.current_tools] == ["done"]


# ── TracingHandler ───────────────────────────────────────────────────────────


class TestTracingHandler:
    def test_silent_without_event_bus(self):
        ctx = _ctx()  # no event_bus
        ctx.actions.append(PusherAction(type=PusherType.NUDGE, kind="reflect",
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
            PusherAction(type=PusherType.NUDGE, kind="reflect",
                         threshold=5, ratio=1.0),
            PusherAction(type=PusherType.FORCE_DONE, kind="time-budget",
                         threshold=1.0, ratio=1.05),
        ]
        TracingHandler().apply(ctx)
        assert len(seen) == 2
        assert seen[0]["action_type"] == "nudge"
        assert seen[0]["kind"] == "reflect"
        assert seen[0]["at"] == 5
        assert seen[1]["action_type"] == "force_done"
        assert seen[1]["kind"] == "time-budget"
        assert seen[1]["at"] == 1.0


# ── BudgetTracker end-to-end ─────────────────────────────────────────────────


class TestBudgetTrackerChain:
    def _tracker(self, *, reflect_interval: int = 0,
                 pushers: list[PusherConfig] | None = None,
                 event_bus: EventBus | None = None):
        cfg = BudgetConfig(
            max_tokens=1000, max_steps=10,
            pushers=pushers or [],
        )
        t = BudgetTracker(cfg, event_bus=event_bus)
        t.configure_reflect_pushers(reflect_interval=reflect_interval)
        return t

    def test_default_chain_has_apply_and_trace_consumers(self):
        t = self._tracker()
        kinds = [h.kind for h in t.handlers]
        # The default chain ships with the counter handler, both
        # always-on per-dimension escalation pushers (token + time),
        # the legacy max_ratio RatioPusher (empty user config), and
        # the two consumers.
        assert "counter" in kinds
        assert "ratio" in kinds
        assert "token-budget" in kinds
        assert "time-budget" in kinds
        assert "apply" in kinds
        assert "trace" in kinds
        # Counter writes ctx.steps_since_reflect → must precede any
        # reader.
        assert kinds.index("counter") < kinds.index("apply")
        # Producers before consumers.
        for k in ("ratio", "token-budget", "time-budget"):
            assert kinds.index(k) < kinds.index("apply")
        assert kinds.index("apply") < kinds.index("trace")

    def test_configure_reflect_pushers_adds_reflect_producer(self):
        t = self._tracker(reflect_interval=5)
        kinds = [h.kind for h in t.handlers]
        assert kinds.count("reflect") == 1
        assert kinds.index("reflect") < kinds.index("apply")

    def test_configure_reflect_pushers_skips_disabled(self):
        t = self._tracker(reflect_interval=0)
        kinds = [h.kind for h in t.handlers]
        assert "reflect" not in kinds

    def _bump_counter(self, tracker, ctx, n_non_reflect: int) -> None:
        """Simulate `n_non_reflect` non-reflect tool calls happening
        between phases, so ReflectCadenceCounter's internal counter
        climbs accordingly. Mirrors what the agent loop does after
        each step's dispatch."""
        ctx.step_outcomes = [("diff_read_file", False)] * n_non_reflect
        tracker.notify_step_done(ctx)
        ctx.step_outcomes = []  # reset for next phase-1 apply

    def test_end_to_end_reflect_nudge_appends_message(self):
        bus = EventBus()
        seen: list[dict] = []
        bus.subscribe(EventType.BUDGET_THRESHOLD_HIT,
                      lambda **kw: seen.append(kw))
        t = self._tracker(reflect_interval=3, event_bus=bus)
        # 3 non-reflect tool calls have run → counter now at 3 →
        # next phase-1 apply should fire NUDGE.
        ctx = _ctx(event_bus=bus)
        self._bump_counter(t, ctx, 3)
        t.apply_handlers(ctx)
        # NUDGE applied → message appended.
        assert len(ctx.messages) == 1
        assert "3 steps without calling reflect" in ctx.messages[0]["content"]
        # Telemetry emitted, tagged reflect.
        assert len(seen) == 1
        assert seen[0]["kind"] == "reflect"
        assert seen[0]["action_type"] == "nudge"

    def test_default_reflect_cadence_does_not_narrow_tools(self):
        """Default `configure_reflect_pushers` registers a
        NUDGE-only cadence handler — FORCE_REFLECT is opt-in. After
        6 non-reflect steps a NUDGE message is appended, but tools
        are NOT narrowed. The narrow-on-FORCE_REFLECT path itself is
        tested in `TestApplyActionsHandler.test_force_reflect_narrows_tools`
        (synthetic action) so the mechanism is still verified."""
        t = self._tracker(reflect_interval=3)
        ctx = _ctx(tools=["reflect", "done", "diff_read_file"])
        self._bump_counter(t, ctx, 6)
        t.apply_handlers(ctx)
        names = [td["function"]["name"] for td in ctx.current_tools]
        # Tools left untouched — model still has everything available.
        assert "diff_read_file" in names
        assert "done" in names
        # The cadence NUDGE was still produced (the message conveys
        # the same urgency without removing tools).
        assert any("without calling reflect" in m["content"]
                    for m in ctx.messages)

    def test_end_to_end_ratio_and_reflect_compose(self):
        """Both a ratio-based pusher and the reflect cadence fire in
        the same step — they share the same actions queue, both get
        applied, both get traced."""
        bus = EventBus()
        seen: list[dict] = []
        bus.subscribe(EventType.BUDGET_THRESHOLD_HIT,
                      lambda **kw: seen.append(kw))
        t = self._tracker(
            reflect_interval=3,
            pushers=[PusherConfig(at=0.5, type=PusherType.NUDGE,
                                  message="halfway through budget")],
            event_bus=bus,
        )
        ctx = _ctx(event_bus=bus)
        ctx.state.cumulative_paid = 800  # token_ratio = 0.8
        self._bump_counter(t, ctx, 3)
        t.apply_handlers(ctx)
        # Both nudges appended.
        contents = [m["content"] for m in ctx.messages]
        assert "halfway through budget" in contents
        # BUDGET_THRESHOLD_HIT events from both kinds.
        kinds = sorted(s["kind"] for s in seen)
        assert "ratio" in kinds
        assert "reflect" in kinds

    def test_counter_resets_on_successful_reflect(self):
        """ReflectCadenceCounter resets on a reflect outcome where
        is_error=False (validation passed, handler ran)."""
        t = self._tracker(reflect_interval=3)
        ctx = _ctx()
        # Walk counter to 5.
        self._bump_counter(t, ctx, 5)
        # Then a step containing a successful reflect + 0 other tools.
        ctx.step_outcomes = [("reflect", False)]
        t.notify_step_done(ctx)
        ctx.step_outcomes = []
        # Next phase-1 apply should see counter=0.
        t.apply_handlers(ctx)
        assert ctx.steps_since_reflect == 0

    def test_counter_does_not_reset_on_failed_reflect(self):
        """A reflect outcome with is_error=True (schema validation
        rejected the args) must NOT reset the counter — otherwise
        the model gets a free pass for malformed reflects and
        cadence pressure never escalates."""
        t = self._tracker(reflect_interval=3)
        ctx = _ctx()
        self._bump_counter(t, ctx, 5)
        # Step containing only a failed reflect (is_error=True).
        ctx.step_outcomes = [("reflect", True)]
        t.notify_step_done(ctx)
        ctx.step_outcomes = []
        t.apply_handlers(ctx)
        assert ctx.steps_since_reflect == 5  # unchanged

    def test_counter_reset_then_increment_in_same_step(self):
        """A step with [reflect_ok, diff_read_file] should land at
        counter=1 (reset + one non-reflect tool)."""
        t = self._tracker(reflect_interval=3)
        ctx = _ctx()
        self._bump_counter(t, ctx, 5)
        ctx.step_outcomes = [("reflect", False), ("diff_read_file", False)]
        t.notify_step_done(ctx)
        ctx.step_outcomes = []
        t.apply_handlers(ctx)
        assert ctx.steps_since_reflect == 1

    def test_done_outcome_does_not_count(self):
        """`done` is the terminal action — it shouldn't move the
        cadence counter."""
        t = self._tracker(reflect_interval=3)
        ctx = _ctx()
        self._bump_counter(t, ctx, 2)
        # Step with only `done` — counter should stay at 2.
        ctx.step_outcomes = [("done", False)]
        t.notify_step_done(ctx)
        ctx.step_outcomes = []
        t.apply_handlers(ctx)
        assert ctx.steps_since_reflect == 2
