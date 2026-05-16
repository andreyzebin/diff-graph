"""End-to-end budget tests with a scripted LLM.

Two providers are simulated:

  - **Uncached** (Qwen-style): cached_tokens=0 on every response.
    `step_paid = prompt + completion`, so cumulative_paid converges
    to the latest step's paid (delta-based accumulation in
    `BudgetTracker.update_tokens`).

  - **Cached** (DeepSeek-style): cached_tokens = ~90% of prompt.
    `step_paid = (prompt - cached) + cached*0.1 + completion`, so
    cumulative_paid grows much more slowly — the agent budget axis
    effectively becomes a per-step paid measure.

The tests:

  1. Single agent — verifies the budget_state evolves as expected
     per step under both providers, AND that the live `budget_stats`
     tool reading reflects the same state at the moment it's called.

  2. Parent + child spawn — verifies the carve-out semantics
     (child gets a slice of parent's remaining) and the elegant
     invariant the budget_stats wording promises: the child's
     internal LLM calls don't grow the parent's own context
     (tokens_in), only the child's `done()` summary returns.

Tool calls (other than `agent_spawn` and `budget_stats`) are mocked
via ToolMocks — the test cares about the BUDGET path, not what
each tool would actually do.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from orchestra.agent import Agent
from orchestra.tool_mocks import ToolMocks
from orchestra.tools.builtin import register_builtins
from orchestra.tools.registry import ToolDef, ToolRegistry
from orchestra.types import AgentConfig, AgentMode, BudgetConfig, LLMParamsConfig


_FIXTURES = Path(__file__).parent / "fixtures" / "budget_e2e"


# ── Mock OpenAI-shaped response ────────────────────────────────────


@dataclass
class _MockFn:
    name: str
    arguments: str


@dataclass
class _MockToolCall:
    id: str
    function: _MockFn
    type: str = "function"


@dataclass
class _MockMessage:
    content: str = ""
    tool_calls: Optional[list] = None


@dataclass
class _MockChoice:
    message: _MockMessage
    finish_reason: str = "tool_calls"
    index: int = 0


@dataclass
class _MockPromptDetails:
    cached_tokens: int


@dataclass
class _MockUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[_MockPromptDetails] = None


@dataclass
class _MockResponse:
    choices: list
    usage: _MockUsage


class _ScriptedCompletions:
    def __init__(self, parent: "ScriptedLLM"):
        self._parent = parent

    def create(self, **kwargs):
        return self._parent._next_response(kwargs)


class _ScriptedChat:
    def __init__(self, parent: "ScriptedLLM"):
        self.completions = _ScriptedCompletions(parent)


class ScriptedLLM:
    """OpenAI-shaped mock. Returns scripted responses in order.

    Each script item: {prompt: int, completion: int, tool_calls: [...]}.
    The `cached_fraction` controls how much of `prompt_tokens` is
    reported as cached — 0.0 mimics Qwen (no cache reporting), 0.9
    mimics DeepSeek-style high cache hit.
    """
    def __init__(self, script: list[dict], *, cached_fraction: float = 0.0):
        self._script = list(script)
        self._cached_fraction = cached_fraction
        self.chat = _ScriptedChat(self)
        self.call_count = 0
        self.recorded_calls: list[dict] = []

    def _next_response(self, kwargs: dict) -> _MockResponse:
        if not self._script:
            raise RuntimeError(
                f"script exhausted at call #{self.call_count} — "
                f"agent kept calling the LLM past the scripted end. "
                f"The test likely needs an extra script entry."
            )
        item = self._script.pop(0)
        self.call_count += 1
        self.recorded_calls.append(kwargs)

        prompt_t = int(item.get("prompt", 0))
        completion_t = int(item.get("completion", 0))
        cached_t = int(prompt_t * self._cached_fraction) if self._cached_fraction > 0 else 0

        tool_calls = []
        for i, tc in enumerate(item.get("tool_calls", []) or []):
            args_json = json.dumps(tc.get("args", {}))
            tool_calls.append(_MockToolCall(
                id=f"call_{self.call_count}_{i}",
                function=_MockFn(name=tc["name"], arguments=args_json),
            ))

        return _MockResponse(
            choices=[_MockChoice(message=_MockMessage(
                content=item.get("content", ""),
                tool_calls=tool_calls or None,
            ))],
            usage=_MockUsage(
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=prompt_t + completion_t,
                prompt_tokens_details=(
                    _MockPromptDetails(cached_tokens=cached_t)
                    if cached_t > 0 else None
                ),
            ),
        )


# ── Agent construction helper ──────────────────────────────────────


def _build_agent(
    *,
    name: str,
    llm,
    script_yaml: dict,
    tools: list[str],
    budget: BudgetConfig,
    agent_configs: Optional[dict] = None,
) -> Agent:
    """Programmatically build an Agent + registry + mocks without
    going through compile_prompts — keeps the test focused on the
    budget path."""
    config = AgentConfig(
        name=name,
        # Minimal system / user prompts — the LLM is scripted so the
        # actual text barely matters, but `_fm_parse` runs on the
        # user_prompt so it has to be valid (empty body is fine).
        system_prompt="You are a test agent.",
        user_prompt="Do the thing.",
        mode=AgentMode.REACT,
        reflect_interval=0,  # disable cadence pusher noise in this test
        tools=tools,
        budget=budget,
        # stream=False so stream_llm hits the non-streaming branch
        # and the ScriptedLLM.create() is what's called.
        llm_params=LLMParamsConfig(stream=False, tool_choice="auto"),
    )

    registry = ToolRegistry()
    # Register a handful of probe domain tools so the script can call
    # them. Real handlers are no-ops — ToolMocks short-circuits them.
    for tool_name in ("diff_list_files", "diff_read_file"):
        if tool_name not in tools:
            continue
        registry.register_tool_def(ToolDef(
            name=tool_name,
            description=f"{tool_name} (test probe)",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda **kw: "(not mocked — should not reach)",
        ))

    # Build the agent first so builtins can capture its reference.
    agent = Agent(
        config=config,
        tool_registry=registry,
        llm=llm,
        model="scripted-mock",
        agent_configs=agent_configs or {},
    )

    # Apply tool mocks from the YAML fixture (excluding agent_spawn /
    # budget_stats — those run for real).
    mocks_src = script_yaml.get("tool_mocks") or {}
    if mocks_src:
        # Filter out the real-must-run tools defensively.
        filtered = {k: v for k, v in mocks_src.items()
                    if k not in ("agent_spawn", "budget_stats")}
        if filtered:
            agent.tool_mocks = ToolMocks.from_dict(filtered)

    # Builtins (done, agent_spawn, budget_stats, agent_list) — these
    # consult tools list + agent reference.
    register_builtins(registry, config, sgr_tracker=None, agent=agent)

    return agent


# ── Test 1: single-agent budget evolution ──────────────────────────


@pytest.fixture
def single_agent_script() -> dict:
    with open(_FIXTURES / "single_agent.yaml") as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("cached_fraction,label", [
    (0.0, "uncached_qwen_style"),
    (0.9, "cached_deepseek_style"),
])
def test_single_agent_budget_evolves_per_step(
    single_agent_script, cached_fraction, label,
):
    """Run a 5-step scripted agent under each provider variant.
    Verifies:
      - All script steps executed (script exhausted).
      - Each step's tokens_in matches the script's prompt count.
      - cumulative_paid trajectory matches the cached/uncached math.
      - context_ratio = tokens_in / max_context at every step.
    """
    llm = ScriptedLLM(single_agent_script["script"],
                       cached_fraction=cached_fraction)
    agent = _build_agent(
        name="probe",
        llm=llm,
        script_yaml=single_agent_script,
        tools=["diff_list_files", "diff_read_file",
               "budget_stats", "reflect", "done"],
        budget=BudgetConfig(
            max_tokens=50_000,
            max_steps=10,
            max_context=20_000,  # so context_ratio is meaningful
        ),
    )

    agent.run()

    # Script exhausted — every step fired.
    assert llm.call_count == 5, (
        f"[{label}] expected 5 LLM calls, got {llm.call_count}"
    )
    # Step counter — 5 LLM calls = 5 steps.
    assert agent.budget_state.steps_used == 5

    # tokens_in tracks the LATEST step's prompt size.
    assert agent.budget_state.tokens_in == 11_000

    # Cumulative-paid math, delta-based. Walk the script and
    # reproduce what BudgetTracker.update_tokens does.
    expected_paid = 0
    prev_step_paid = 0
    for item in single_agent_script["script"]:
        prompt_t = item["prompt"]
        completion_t = item["completion"]
        cached_t = int(prompt_t * cached_fraction)
        step_paid = (
            max(0, prompt_t - cached_t)
            + int(cached_t * 0.1)
            + completion_t
        )
        delta = step_paid - prev_step_paid
        if delta < 0:
            delta = step_paid
        expected_paid += delta
        prev_step_paid = step_paid
    assert agent.budget_state.cumulative_paid == expected_paid, (
        f"[{label}] cumulative_paid drift"
    )

    # context_ratio = tokens_in / max_context.
    assert agent.budget_state.context_ratio == pytest.approx(
        11_000 / 20_000
    )


# ── Test 2: budget_stats output reflects live state ────────────────


def test_budget_stats_tool_returns_live_state(single_agent_script):
    """The agent calls `budget_stats()` at step 2. The string the
    tool returns to the LLM (visible in the NEXT step's messages as
    a `role=tool` entry) MUST reflect the budget state at the moment
    the tool ran — not the initial zero, not the final state. This
    pins the live-reading contract.

    Inspection technique: `ScriptedLLM.recorded_calls` captures the
    exact `messages` array submitted on each call. Step 2's
    budget_stats result is in the messages passed to step 3."""
    llm = ScriptedLLM(single_agent_script["script"], cached_fraction=0.0)
    agent = _build_agent(
        name="probe",
        llm=llm,
        script_yaml=single_agent_script,
        tools=["diff_list_files", "diff_read_file",
               "budget_stats", "reflect", "done"],
        budget=BudgetConfig(
            max_tokens=50_000,
            max_steps=10,
            max_context=20_000,
        ),
    )
    agent.run()

    # Step 3's LLM call is the first one to include the budget_stats
    # tool result (the result of step 2's call). The compact-form
    # output starts each row with `own / shared / wall / spawn:`
    # prefixes — grep on the row prefix instead of free-text
    # because the table-with-bars layout dropped the old prose.
    step3_messages = llm.recorded_calls[3]["messages"]
    tool_results = [
        m for m in step3_messages
        if m.get("role") == "tool"
        and "spawn:" in str(m.get("content", ""))
        and "own    " in str(m.get("content", ""))
    ]
    assert tool_results, "budget_stats should have produced a tool-result"
    body = tool_results[-1]["content"]

    # At dispatch time of step 2, the agent had just done its 3rd
    # update_tokens (steps_used was bumped to 3) and tokens_in was
    # set from the step-2 prompt = 8000. So budget_stats reads
    # tokens_in=8000, max_context=20000 → ratio 40%, steps=3.
    assert "8K" in body, body
    assert "40%" in body, body
    # Shared-pool steps live in the `(steps N/M)` parenthetical on
    # the shared line of the compact table.
    assert "(steps 3/10)" in body, body


# ── Test 3: parent + child spawn ───────────────────────────────────


@pytest.fixture
def spawn_script() -> dict:
    with open(_FIXTURES / "parent_child_spawn.yaml") as f:
        return yaml.safe_load(f)


def test_parent_child_spawn_budget_split(spawn_script):
    """End-to-end spawn test. The parent and child share a single
    ScriptedLLM that serves responses in dispatch order: parent
    step 0 → parent step 1 (spawn) → child step 0 → child step 1
    → parent step 2 → parent step 3. The KEY observation: each
    agent's React loop is what calls `update_tokens` on its OWN
    BudgetState, so even though they share the LLM instance, their
    budget accounting is independent.

    Verifies:
      - All 6 LLM calls fired (script exhausted).
      - Parent's tokens_in tracks ONLY its own LLM calls (final
        value = parent's last step's prompt). The child's 1500 /
        2200 prompts went into the CHILD's BudgetState.
      - Parent's steps_used = 4 (parent only).
      - Child ran (visible via the spawn AgentSpawned event AND the
        captured child_id on the parent).
    """
    llm = ScriptedLLM(spawn_script["script"], cached_fraction=0.0)

    child_config = AgentConfig(
        name="child",
        system_prompt="You are a child agent.",
        user_prompt="Investigate {focus}.",
        mode=AgentMode.REACT,
        reflect_interval=0,
        tools=["diff_read_file", "done"],
        budget=BudgetConfig(max_tokens=50_000, max_steps=10),
        llm_params=LLMParamsConfig(stream=False, tool_choice="auto"),
    )

    parent = _build_agent(
        name="parent",
        llm=llm,
        script_yaml=spawn_script,
        tools=["budget_stats", "agent_spawn", "done"],
        budget=BudgetConfig(
            max_tokens=50_000,
            max_steps=10,
            max_context=20_000,
        ),
        agent_configs={"child": child_config},
    )

    parent.run()

    # All 6 script entries consumed.
    assert llm.call_count == 6, (
        f"expected 6 LLM calls (4 parent + 2 child), got {llm.call_count}"
    )

    # Parent's own session tokens_in = parent's LAST script prompt.
    # The child's 1500 / 2200 went into the CHILD's BudgetState, not
    # the parent's — that's the key isolation we're pinning.
    assert parent.budget_state.tokens_in == 9_500, (
        f"parent tokens_in leaked from child? got {parent.budget_state.tokens_in}"
    )

    # Parent took 4 of the 6 LLM calls; child took the other 2.
    assert parent.budget_state.steps_used == 4

    # Spawn actually happened — a child agent was registered on the
    # parent. (Accessing _children directly because that's the
    # in-process record kept by _meta_agent_spawn.)
    assert len(parent._children) == 1, (
        f"expected 1 spawned child, got {len(parent._children)}"
    )
    child_agent = next(iter(parent._children.values()))
    assert child_agent.config.name == "child"
    # Child has its OWN BudgetState with its OWN counters.
    assert child_agent.budget_state is not parent.budget_state
    assert child_agent.budget_state.steps_used == 2
    assert child_agent.budget_state.tokens_in == 2_200

    # Parent's second `budget_stats` call (script step 4 in parent
    # space, LLM call #4 across the shared script) happens AFTER
    # the spawn returned. Its rendered output must include the
    # subagents block listing the completed child's stats.
    # Inspect the LLM call AFTER that one (#5 = done step) — its
    # messages array carries the budget_stats tool_result.
    step5_messages = llm.recorded_calls[5]["messages"]
    bs_results = [
        m for m in step5_messages
        if m.get("role") == "tool" and "Subagents" in str(m.get("content", ""))
    ]
    assert bs_results, (
        "parent's post-spawn budget_stats should have surfaced a "
        "Subagents block — none found in step-5 messages"
    )
    body = bs_results[-1]["content"]
    assert "Subagents (1 spawned)" in body
    assert "child [completed]" in body
    assert "2 steps" in body
    assert 'focus="look at A.java"' in body


# ── Test 4: pushers fire end-to-end ────────────────────────────────


def test_token_budget_nudge_fires_into_messages(single_agent_script):
    """End-to-end pusher verification: with `max_tokens` set low
    enough that the token NUDGE crosses at step ~2, the nudge
    message must appear in the SUBSEQUENT step's LLM call as a
    `role=user` message. This is the full pipeline:
    `TokenBudgetPusher` produces a NUDGE action →
    `ApplyActionsHandler` appends to ctx.messages → next LLM call
    sees it. Catches regressions in any of those layers."""
    llm = ScriptedLLM(single_agent_script["script"], cached_fraction=0.0)
    agent = _build_agent(
        name="probe",
        llm=llm,
        script_yaml=single_agent_script,
        tools=["diff_list_files", "diff_read_file",
               "budget_stats", "reflect", "done"],
        # max_tokens low enough that step 0's paid (4000+80=4080)
        # is already past the 50% NUDGE threshold (4000). The
        # NUDGE should appear in messages from step 1 onward.
        budget=BudgetConfig(max_tokens=8_000, max_steps=10),
    )
    agent.run()

    # Walk every recorded LLM-call messages, look for the token-
    # budget NUDGE phrasing. Defined in orchestra/messages.yaml
    # (`budget.token.nudge`).
    seen_nudge = False
    for call in llm.recorded_calls:
        for m in call.get("messages", []):
            if m.get("role") == "user" and "token budget" in str(
                m.get("content", "")
            ).lower():
                seen_nudge = True
                break
        if seen_nudge:
            break
    assert seen_nudge, (
        "TokenBudgetPusher NUDGE never made it into a subsequent "
        "LLM call's messages — check the producer → ApplyActionsHandler "
        "→ ctx.messages pipeline"
    )


def test_full_pusher_pipeline_through_force_done():
    """Kitchen-sink: ONE 10-step scripted run drives every default
    pusher across at least one threshold. Asserts each pusher's
    expected action actually lands in the agent's runtime.

    Pusher coverage in this single run:
      - ReflectCadencePusher       → NUDGE at step 3 apply
        (steps_since_reflect=3, threshold=3)
      - TokenBudgetPusher          → NUDGE at step 5 apply
        (token_ratio crosses 50%)
      - ContextBudgetPusher        → NUDGE at step 5 apply
        (context_ratio crosses 50%)
      - StepBudgetPusher           → NUDGE at step 5 apply
        (step_ratio crosses 50%)
      - StepBudgetPusher           → FORCE_DONE at step 9 apply
        (step_ratio = 90%) — narrows ctx.current_tools to [done]
        BEFORE the step's LLM call, so the recorded tools schema
        for call #9 contains only `done`.

    Not covered here (separate concerns):
      - TimeBudgetPusher           — needs max_wall_time + actual
                                     elapsed time; unit-tested in
                                     test_budget.py.
      - TokenBudgetPusher FORCE_DONE — would terminate the run
                                       earlier than step 9; this
                                       script keeps token_ratio
                                       under 100% so the cleaner
                                       step-axis FORCE_DONE fires.
      - ContextBudgetPusher        — NUDGE-only by design (see
                                     docs/orchestra-architecture.md).
      - FailedReflectGuard         — not in default chain.
      - RatioPusher (yaml)         — empty by default.
    """
    with open(_FIXTURES / "all_pushers.yaml") as f:
        script_yaml = yaml.safe_load(f)
    llm = ScriptedLLM(script_yaml["script"], cached_fraction=0.0)
    agent = _build_agent(
        name="probe",
        llm=llm,
        script_yaml=script_yaml,
        tools=["diff_list_files", "diff_read_file",
               "reflect", "done"],
        budget=BudgetConfig(
            max_tokens=15_000,
            max_steps=10,
            max_context=15_000,
            # No max_wall_time — TimeBudgetPusher stays silent.
        ),
    )
    # Reflect cadence interval = 3. The default _build_agent path
    # uses reflect_interval=0 (silent), so override here. We need
    # `reflect` in tools for the cadence handler to register.
    agent.config.reflect_interval = 3
    agent.budget_tracker.configure_reflect_pushers(reflect_interval=3)

    agent.run()

    # All 10 script entries consumed → loop ran end-to-end and the
    # final done() terminated it cleanly via the narrowed tool surface.
    assert llm.call_count == 10, (
        f"expected 10 LLM calls; got {llm.call_count}"
    )

    # ── NUDGE coverage ─────────────────────────────────────────
    # Walk every LLM call's messages, collect lowercased user-role
    # bodies, search for each pusher's signature phrase.
    all_user_bodies: list[str] = []
    for call in llm.recorded_calls:
        for m in call.get("messages", []):
            if m.get("role") == "user":
                all_user_bodies.append(str(m.get("content", "")).lower())

    # Pusher → substring that uniquely identifies its NUDGE wording
    # (lives in orchestra/messages.yaml — these substrings must stay
    # in sync if the YAML wording is retuned).
    expected_nudges = {
        "TokenBudgetPusher": "token budget",
        "StepBudgetPusher":  "step budget",
        "ContextBudgetPusher": "context window",
        "ReflectCadencePusher": "without calling reflect",
    }
    for pusher_name, needle in expected_nudges.items():
        assert any(needle in body for body in all_user_bodies), (
            f"{pusher_name} NUDGE never landed in any LLM call's "
            f"user messages — searched for {needle!r}. "
            f"Collected {len(all_user_bodies)} user bodies."
        )

    # ── NUDGE_HIGH coverage ────────────────────────────────────
    # Second-level NUDGE @ 0.75 (mandatory warning before FORCE_DONE)
    # for token/step/context. Wording distinguishes by "most of" or
    # "75%" (context). Token/step messages share "wrap up soon"
    # phrasing; context keeps pure-state "75% of …".
    # Step ratio at step 8 apply = 80%, token = 81%, context = 80% —
    # all cross NUDGE_HIGH. So all three should appear by run end.
    nudge_high_signatures = {
        "TokenBudgetPusher NUDGE_HIGH":  ("most of your token", "wrap up soon"),
        "StepBudgetPusher NUDGE_HIGH":   ("most of your step",  "wrap up soon"),
        "ContextBudgetPusher NUDGE_HIGH": ("conversation history fills 75%",),
    }
    for pusher_name, needles in nudge_high_signatures.items():
        assert any(
            all(needle in body for needle in needles)
            for body in all_user_bodies
        ), (
            f"{pusher_name} never landed in any LLM call's user "
            f"messages — searched for all of {needles!r}."
        )

    # ── FORCE_DONE coverage ────────────────────────────────────
    # StepBudgetPusher's FORCE_DONE fires at step 9 apply (ratio=90%).
    # ApplyActionsHandler narrows ctx.current_tools to [done], and
    # the LLM call's `tools` kwarg is built from ctx.current_tools.
    # So recorded_calls[9]["tools"] must contain only one entry, named
    # "done".
    step9_tools = llm.recorded_calls[9].get("tools") or []
    tool_names = [t.get("function", {}).get("name") for t in step9_tools]
    assert tool_names == ["done"], (
        f"step 9 LLM call should have been narrowed to [done] by "
        f"StepBudgetPusher's FORCE_DONE; got tools={tool_names}"
    )
    # Earlier calls had a richer tool surface — sanity check.
    step0_tools = llm.recorded_calls[0].get("tools") or []
    step0_names = sorted(t.get("function", {}).get("name") for t in step0_tools)
    assert "done" in step0_names
    # At step 0, none of the budget axes have triggered yet, so the
    # full domain surface is visible.
    assert len(step0_names) > 1, (
        f"step 0 should expose the full tool surface, got {step0_names}"
    )


def test_context_budget_nudge_fires_into_messages(single_agent_script):
    """Sibling test for the context axis. With `max_context` set
    low enough that step 0's tokens_in=4000 is already past the
    50% NUDGE threshold (2000), the context NUDGE — pure-state
    wording from `orchestra/messages.yaml` `budget.context.nudge`
    — should appear in step 1's messages."""
    llm = ScriptedLLM(single_agent_script["script"], cached_fraction=0.0)
    agent = _build_agent(
        name="probe",
        llm=llm,
        script_yaml=single_agent_script,
        tools=["diff_list_files", "diff_read_file",
               "budget_stats", "reflect", "done"],
        budget=BudgetConfig(
            max_tokens=100_000,    # huge — token pusher silent
            max_steps=100,
            # max_context=12K. Script tokens_in walk: 4K → 5.5K →
            # 8K → 9.5K → 11K. context_ratio crosses the 0.5 NUDGE
            # threshold at step 2's tokens_in=8K (67%) — so step 3's
            # apply_handlers fires the NUDGE into messages. Never
            # hits 1.0 (max is 11K/12K=92%), so `state.exhausted`
            # stays False and the loop runs to done().
            max_context=12_000,
        ),
    )
    agent.run()

    seen_nudge = False
    for call in llm.recorded_calls:
        for m in call.get("messages", []):
            content = str(m.get("content", "")).lower()
            if m.get("role") == "user" and (
                "context" in content and "window" in content
            ):
                seen_nudge = True
                break
        if seen_nudge:
            break
    assert seen_nudge, (
        "ContextBudgetPusher NUDGE never made it into a subsequent "
        "LLM call's messages"
    )
