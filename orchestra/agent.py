"""
Agent: the only execution unit.

Two modes:
  - single: one LLM call, no tools
  - react:  non-deterministic ReAct loop with tools, SGR, budget, condensation

The agent manages its own children (spawn, fork). No external topology runner.
LLM params are mutable state controllable by parent agents via adjust_agent.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import AgentConfig, AgentMode, BudgetConfig, LLMParamsConfig
from .budget import BudgetTracker, BudgetState
from .events import EventBus, EventType
from .sgr import SGRTracker, SGREntry
from .condensation import get_condenser, should_condense
from .streaming import stream_llm
from .prompts import load_prompt, load_internal
from .tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# Strip C0 control chars except \t, \n, \r before sending to LLM API.
# Some endpoints reject any other control char in JSON string fields with
# 400 "Invalid control character".
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_tool_content(content: str) -> str:
    if not isinstance(content, str):
        return content
    return _CTRL_CHARS_RE.sub("", content)


@dataclass
class StepRecord:
    step: int
    tool_calls: list[dict] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    llm_params: dict = field(default_factory=dict)


@dataclass
class AgentTrace:
    agent_id: str = ""
    agent_name: str = ""
    parent_id: Optional[str] = None
    steps: list[StepRecord] = field(default_factory=list)
    total_tokens: int = 0
    total_steps: int = 0


@dataclass
class AgentResult:
    agent_id: str = ""
    agent_name: str = ""
    output: Any = None
    sgr_history: list[SGREntry] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    budget_state: Optional[BudgetState] = None
    trace: AgentTrace = field(default_factory=AgentTrace)


class Agent:
    """
    Core agent. Manages its own children, mutable LLM params, SGR,
    budget, and condensation. No external runner needed.
    """

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        llm: Any,
        model: str,
        event_bus: Optional[EventBus] = None,
        agent_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        parent: Optional["Agent"] = None,
        depth: int = 0,
        context_messages: Optional[list[dict]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
        agent_configs: Optional[dict[str, AgentConfig]] = None,
        agent_registry: Any = None,  # AgentRegistry from compiler
        tool_mocks: Any = None,      # ToolMocks; intercepts _handle_tool_call generically
        user_message_override: Optional[str] = None,
        task_message_override: Optional[str] = None,
    ) -> None:
        self.config = config
        self.registry = tool_registry
        self.llm = llm
        self.model = model
        self.event_bus = event_bus or EventBus()
        self.agent_id = agent_id or str(uuid.uuid4())[:8]
        self.parent_id = parent_id
        self.parent = parent
        self.depth = depth
        self.context_messages = context_messages or []
        self.prompt_vars = prompt_vars or {}
        self.agent_configs = agent_configs or {}
        self.agent_registry = agent_registry  # compiled prompt registry
        # Inherited from parent so deep tool calls in spawned children are
        # still intercepted when the fixture covers them.
        self.tool_mocks = tool_mocks if tool_mocks is not None else (parent.tool_mocks if parent else None)
        # Optional per-run override of the user_prompt template. Used
        # by unit tests (consolidation-only reviewer call etc.) and
        # by parent spawns that want to reframe the child's task. None
        # means "use config.user_prompt".
        self.user_message_override = user_message_override
        # Optional per-run override of the task body — the smaller-
        # blast-radius testing extension point. When set, the
        # `{task}` placeholder in config.user_prompt is filled with
        # this value instead of config.task_prompt. Environment
        # framing (PR meta, diff hint, commits) in user_prompt
        # stays shared with production. Mutually safe with
        # user_message_override: if user_message_override is set it
        # wins completely (legacy full-message swap).
        self.task_message_override = task_message_override
        self.data_scope: dict[str, str] = {}  # resolved @data values for inheritance

        # Parse frontmatter on whichever user message is active for
        # this run (override > config). Captures dispatch_mode +
        # tool subset + extra_tools (capture-style schemas). The
        # body becomes the user-facing prompt; the meta drives
        # _build_tool_names and registry extras below.
        from .prompts.frontmatter import parse as _fm_parse, validate as _fm_validate
        _src = (self.user_message_override
                if self.user_message_override is not None
                else (self.config.user_prompt or ""))
        try:
            _fm = _fm_parse(_src)
            # User layer = the per-run override / test prompt. Validator
            # enforces `tools_add`-only (no full-replace `tools`) here.
            _fm_validate(_fm, role="user")
        except ValueError as exc:
            log.error("frontmatter error on agent %s: %s", self.config.name, exc)
            raise
        self._fm_meta: dict = _fm.meta
        self._fm_body: str = _fm.body

        # Mount skills FIRST. mount_skills mutates _fm_meta to
        # extend tools_add / extra_tools / flags. Running before
        # the flags-merge block below means the override block
        # sees the merged view (user prompt wins, but absent
        # keys fall back to the skill's defaults).
        _skill_names = self._fm_meta.get("skills") or []
        if _skill_names and not isinstance(_skill_names, list):
            raise ValueError(
                f"frontmatter.skills must be a list of strings, "
                f"got {type(_skill_names).__name__}"
            )
        from .skills import mount_skills as _mount_skills
        self._mounted_skills_body: str = _mount_skills(
            [str(s) for s in _skill_names],
            fm_meta=self._fm_meta,
        )

        # Merge per-area frontmatter blocks (`reflect:`, ...) into
        # config dicts. Per-key override: anything declared in the
        # user-message frontmatter (or supplied by a mounted skill)
        # wins over the base-prompt config. Adding a new per-area
        # namespace = append its name here.
        for _area in ("reflect",):
            _fm_block = self._fm_meta.get(_area)
            if isinstance(_fm_block, dict):
                merged = dict(getattr(self.config, _area, None) or {})
                merged.update(_fm_block)
                setattr(self.config, _area, merged)
            elif _fm_block is not None:
                raise ValueError(
                    f"frontmatter.{_area} must be a mapping, "
                    f"got {type(_fm_block).__name__}"
                )

        # Same override channel for `budget:` — bench / test prompts can
        # tighten the context window (or any budget axis) per scenario
        # without touching the production prompt OR plumbing env vars
        # through cli + run_unit. Dict form mirrors BudgetConfig field
        # names so it's "just the YAML BudgetConfig". Only listed
        # fields get overridden; unspecified ones keep the base value.
        _fm_budget = self._fm_meta.get("budget")
        if isinstance(_fm_budget, dict):
            for _k in ("max_tokens", "max_steps", "max_context"):
                if _k in _fm_budget and _fm_budget[_k] is not None:
                    setattr(self.config.budget, _k, int(_fm_budget[_k]))
            if "max_wall_time" in _fm_budget and _fm_budget["max_wall_time"] is not None:
                self.config.budget.max_wall_time = float(_fm_budget["max_wall_time"])

        # Register extra_tools as capture-style tools in the agent's
        # registry. Idempotent — re-registering the same name on a
        # spawned child just overwrites.
        for spec in self._fm_meta.get("extra_tools", []) or []:
            self.registry.register_capture_tool(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
            )

        # SGR
        self.sgr = SGRTracker(config.sgr_extensions) if "reflect" in config.tools else None

        # Budget
        self.budget_tracker = BudgetTracker(config.budget, self.event_bus)
        # Slot the step-cadence reflect producer into the pusher
        # pipeline; no-op when reflect_interval <= 0 or the agent has
        # no reflect tool. Wall-clock reflect-pressure is handled by
        # the always-on TimeBudgetPusher (NUDGE/FORCE_REFLECT/FORCE_DONE
        # at fractions of max_wall_time), not a fixed-interval cadence
        # on wall clock.
        self.budget_tracker.configure_reflect_pushers(
            reflect_interval=(
                int(config.reflect.get("interval", 3)) if self.sgr else 0
            ),
        )
        self.budget_state: Optional[BudgetState] = None

        # Latest in-flight step number. Set at the top of every loop
        # iteration so tool handlers (reflect → AGENT_REFLECT emit,
        # ...) can stamp step on their events without threading it
        # through argument lists. The reflect-cadence counter itself
        # lives on `ReflectCadenceCounter` in the pusher pipeline
        # (orchestra/budget.py) — not on the agent.
        self._current_step: int = -1

        # Mutable LLM params — can be changed at any time by self or parent
        self.llm_params = self._init_llm_params()
        self._params_lock = threading.Lock()

        # Children tracking
        self._children: dict[str, "Agent"] = {}  # agent_id -> Agent
        self._children_results: dict[str, AgentResult] = {}
        self._children_lock = threading.Lock()

        # Done state
        self._done_output: Any = None
        self._done_called = False

        # Injected messages queue (from adjust_agent by parent)
        self._injected_messages: list[str] = []
        self._inject_lock = threading.Lock()

    def _init_llm_params(self) -> dict[str, Any]:
        """Initialize mutable LLM params from config."""
        lp = self.config.llm_params or LLMParamsConfig()
        params: dict[str, Any] = {"temperature": lp.temperature}
        if lp.top_p != 1.0:
            params["top_p"] = lp.top_p
        if lp.frequency_penalty:
            params["frequency_penalty"] = lp.frequency_penalty
        if lp.presence_penalty:
            params["presence_penalty"] = lp.presence_penalty
        if lp.max_completion_tokens:
            params["max_completion_tokens"] = lp.max_completion_tokens
        if lp.model:
            params["model"] = lp.model
        if lp.tool_choice and lp.tool_choice != "required":
            params["tool_choice"] = lp.tool_choice
        # Backend-specific knobs from the provider profile.
        if lp.stream is False:
            params["stream"] = False
        if lp.extra_body:
            params["extra_body"] = lp.extra_body
        return params

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> AgentResult:
        """Main entry point. Wrapped in one OTel span — same shape
        for root agent (called from cli.py) and spawned children
        (called from agent_spawn), so the trace tree is uniform.
        """
        self.event_bus.emit(EventType.AGENT_STARTED,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           parent_id=self.parent_id, depth=self.depth)
        try:
            from opentelemetry import trace as _otel_trace
            from .otel import get_domain_attrs as _get_domain
            _tracer = _otel_trace.get_tracer("diffgraph-agent")
            # Merge domain dims (scenario_id / mutation / plan_id /
            # task_id) set by cli.session — so /qa/traces can filter
            # via single SELECT on otel_spans without any JOIN.
            _attrs = {
                **_get_domain(),
                "diffgraph.agent_id":   self.agent_id,
                "diffgraph.agent_name": self.config.name,
                "diffgraph.depth":      self.depth,
                "diffgraph.parent_id":  self.parent_id or "",
                "diffgraph.mode":       self.config.mode.name,
            }
            _span_cm = _tracer.start_as_current_span(
                f"agent.{self.config.name}", attributes=_attrs,
            )
        except Exception:
            _span_cm = None
        try:
            if _span_cm is not None:
                with _span_cm:
                    if self.config.mode == AgentMode.SINGLE:
                        return self._run_single()
                    return self._run_react()
            if self.config.mode == AgentMode.SINGLE:
                return self._run_single()
            return self._run_react()
        except Exception:
            raise

    def get_status(self) -> dict:
        """Return current status for observe_agents."""
        status = {
            "agent_id": self.agent_id,
            "agent_name": self.config.name,
            "status": "done" if self._done_called else "running",
            "step": self.budget_state.steps_used if self.budget_state else 0,
            "budget_ratio": self.budget_state.max_ratio if self.budget_state else 0.0,
            "llm_params": dict(self.llm_params),
        }
        if self.sgr and self.sgr.last:
            status["sgr"] = {
                "confidence": self.sgr.last.confidence,
                "questions_remaining": len(self.sgr.last.questions_remaining),
                "learned": self.sgr.last.learned[:300],
            }
        return status

    def adjust_params(self, changes: dict, source: str = "unknown") -> dict:
        """Adjust mutable LLM params. Called by parent via adjust_agent tool."""
        applied = {}
        with self._params_lock:
            for key, value in changes.items():
                if key in ("temperature", "top_p", "frequency_penalty",
                           "presence_penalty", "max_completion_tokens", "model"):
                    # Clamp
                    if key == "temperature":
                        value = max(0.0, min(2.0, float(value)))
                    elif key in ("frequency_penalty", "presence_penalty"):
                        value = max(-2.0, min(2.0, float(value)))
                    elif key == "top_p":
                        value = max(0.0, min(1.0, float(value)))

                    old = self.llm_params.get(key)
                    self.llm_params[key] = value
                    applied[key] = {"old": old, "new": value}

                    self.event_bus.emit(EventType.PARAM_ADJUSTED,
                                       agent_id=self.agent_id,
                                       param=key, old_value=old, new_value=value,
                                       source=source)
        return applied

    def inject_message(self, message: str) -> None:
        """Queue a message to be injected into agent's context. Thread-safe."""
        with self._inject_lock:
            self._injected_messages.append(message)

    # ── Single-shot mode ──────────────────────────────────────────────────────

    def _observe_llm_call(self, *, step: int, messages: list,
                          tools: Optional[list], do_call: Callable,
                          mode: str = "react") -> Any:
        """Shared LLM-call wrapper used by both _run_single and
        _run_react. Owns the OTel `llm.request` span, request +
        response payload stashing, and usage stamping.

        Caller owns event-bus emits (AGENT_LLM_REQUEST before,
        AGENT_LLM_RESPONSE + AGENT_STEP after) because the event
        payload shape differs slightly per mode (single has no
        tool_calls; react has tool dispatch metadata) and we want
        the helper kind-neutral.

        Raises whatever `do_call()` raises — caller is responsible
        for catching and emitting AGENT_LLM_ERROR.
        """
        model = self.llm_params.get("model", self.model)
        try:
            from .otel import observe as _observe
            from .otel_fs import (stash_request as _stash_req,
                                   stash_response as _stash_res)
            attrs = {
                "llm.model": model,
                "llm.message_count": len(messages),
                "diffgraph.agent_id":   self.agent_id,
                "diffgraph.agent_name": self.config.name,
                "diffgraph.mode":       mode,
                "diffgraph.step":       step,
            }
            if tools is not None:
                attrs["llm.tools_count"] = len(tools)
            _llm_ctx = _observe("llm.request", attrs)
        except Exception:
            _llm_ctx = None
            _stash_req = _stash_res = lambda *a, **kw: None
        req_payload = {"model": model, "messages": messages,
                       "params": dict(self.llm_params)}
        if tools is not None:
            req_payload["tools"] = tools
        if _llm_ctx is not None:
            with _llm_ctx as _llm_span:
                _stash_req(req_payload)
                response = do_call()
                # Both stream_llm (ReAct) and a plain non-stream
                # OpenAI call (single) shape `.choices[0].message`
                # uniformly. The stash payload below mirrors what
                # the AI debugger reads back via span_id.
                msg = response.choices[0].message
                _u = response.usage
                _usage_dict = ({
                    "prompt_tokens": _u.prompt_tokens,
                    "completion_tokens": _u.completion_tokens,
                    "cached_tokens": _extract_cached(_u),
                } if _u else {})
                _stash_res({
                    "content": getattr(msg, "content", "") or "",
                    "tool_calls": [
                        {"name": tc.function.name,
                         "arguments": tc.function.arguments}
                        for tc in (getattr(msg, "tool_calls", None) or [])
                    ],
                    "usage": _usage_dict,
                })
                _stamp_usage(_llm_span, response)
                return response
        return do_call()

    def _run_single(self) -> AgentResult:
        messages = self._build_messages()
        model = self.llm_params.get("model", self.model)
        # Symmetric with _run_react step 0: emit AGENT_LLM_REQUEST
        # BEFORE the call so the events table has the same step
        # shape for mode:single agents (judges, lead agents that
        # don't loop) as it does for ReAct agents. Without this,
        # single-shot runs had only `agent_started` in their events
        # stream and /qa/sessions showed "no steps".
        self.event_bus.emit(EventType.AGENT_LLM_REQUEST,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           step=0, messages=messages, tools=None,
                           llm_params={"model": model, **dict(self.llm_params)})
        try:
            from .streaming import _llm_call_with_retry
            def _do_call():
                return _llm_call_with_retry(lambda: self.llm.chat.completions.create(
                    model=model, messages=messages,
                    temperature=self.llm_params.get("temperature", 0),
                    stream=False,
                ))
            response = self._observe_llm_call(
                step=0, messages=messages, tools=None,
                do_call=_do_call, mode="single",
            )
            content = (response.choices[0].message.content or "").strip()
            output = _try_parse_json(content)
            # Emit AGENT_LLM_RESPONSE + AGENT_STEP — same shape as
            # a text-only ReAct step (tool_calls=[]), so the diagram
            # walker renders the lone step generically (tool_call to
            # system:human, no kind-special case).
            usage = response.usage
            tok_in = usage.prompt_tokens if usage else 0
            tok_out = usage.completion_tokens if usage else 0
            tok_cached = _extract_cached(usage) if usage else 0
            self.event_bus.emit(EventType.AGENT_LLM_RESPONSE,
                               agent_id=self.agent_id, agent_name=self.config.name,
                               step=0,
                               tool_calls=[],
                               content=content,
                               usage={"prompt_tokens": tok_in,
                                      "completion_tokens": tok_out,
                                      "cached_tokens": tok_cached,
                                      "paid": max(0, tok_in - tok_cached) + tok_out})
            self.event_bus.emit(EventType.AGENT_STEP,
                               agent_id=self.agent_id, agent_name=self.config.name,
                               step=0,
                               tool="(text)",
                               tools=[],
                               text_preview=content[:100].replace("\n", " "),
                               args={},
                               tok_in=tok_in,
                               tok_out=tok_out)
        except Exception as exc:
            log.warning("single-shot agent '%s' failed: %s", self.config.name, exc)
            output = None
            # Mark the surrounding agent.<name> span as ERROR so
            # /qa/traces shows ✗ failed instead of ✓ completed. The
            # exception is swallowed for callers (we still return an
            # AgentResult), but observability has to surface it —
            # judges that 401/timeout otherwise look indistinguishable
            # from successful no-op runs.
            try:
                from opentelemetry import trace as _otel_trace
                from opentelemetry.trace import StatusCode, Status as _Status
                _cur = _otel_trace.get_current_span()
                if _cur is not None:
                    _cur.record_exception(exc)
                    _cur.set_status(_Status(StatusCode.ERROR,
                                             f"{type(exc).__name__}: {exc}"[:300]))
            except Exception:
                pass
            # Emit on the event bus too so /qa/sessions has a terminal
            # marker. See the equivalent in _run_react for full rationale.
            try:
                self.event_bus.emit(
                    EventType.AGENT_LLM_ERROR,
                    agent_id=self.agent_id,
                    agent_name=self.config.name,
                    step=0,
                    error_class=type(exc).__name__,
                    error_message=f"{exc}"[:1000],
                    model=self.llm_params.get("model", self.model),
                )
            except Exception:
                pass

        # Emit AGENT_DONE so the events stream has the same terminal
        # marker shape as ReAct agents — symmetric with the
        # AGENT_LLM_RESPONSE/AGENT_STEP pair above. Without this
        # /qa/sessions sees the run as "still loading" until the
        # orphan sweeper closes it.
        try:
            self.event_bus.emit(EventType.AGENT_DONE,
                               agent_id=self.agent_id,
                               agent_name=self.config.name,
                               output=output)
        except Exception:
            pass

        return AgentResult(
            agent_id=self.agent_id,
            agent_name=self.config.name,
            output=output,
            messages=messages,
            trace=AgentTrace(agent_id=self.agent_id, agent_name=self.config.name,
                             parent_id=self.parent_id),
        )

    # ── ReAct loop ────────────────────────────────────────────────────────────

    def _run_react(self) -> AgentResult:
        messages = self._build_messages()
        self.budget_state = self.budget_tracker.start()
        trace = AgentTrace(agent_id=self.agent_id, agent_name=self.config.name,
                           parent_id=self.parent_id)

        tool_names = self._build_tool_names()
        tools_schema = self.registry.to_openai_schema(tool_names)
        self._natural_stop = False
        _guard_retries: dict[str, int] = {}  # guard_name → retry count
        _MAX_GUARD_RETRIES = 2
        _called_tools: set[str] = set()  # track all tools called during this run

        for step in range(self.config.budget.max_steps):
            # Stash the current step for tool handlers that need it
            # (reflect → SGR record, AGENT_REFLECT event, …). Tool
            # handlers in tools/builtin.py read `agent._current_step`
            # rather than receiving step through args, so all tools
            # share the same `handler(**args)` signature.
            self._current_step = step
            if self.budget_state.exhausted:
                self.event_bus.emit(EventType.AGENT_FORCED_DONE,
                                   agent_id=self.agent_id, agent_name=self.config.name, reason="token limit",
                                   tok_in=self.budget_state.tokens_in,
                                   tok_out=self.budget_state.tokens_out,
                                   tok_cached=self.budget_state.tokens_cached)
                break

            # Drain injected messages from parent
            self._drain_injected(messages)

            # Pusher pipeline (see orchestra/budget.py). Build a step
            # context, hand it to the chain. Producers (ratio,
            # time-budget, sgr-cadence) append actions;
            # ApplyActionsHandler mutates `messages` and
            # `ctx.current_tools` in place; TracingHandler emits
            # BUDGET_THRESHOLD_HIT events. We then read
            # `ctx.current_tools` for the LLM call.
            # `steps_since_reflect` is written into ctx by
            # ReflectCadenceCounter's `apply` (it owns the counter
            # state across steps). We just construct ctx with the
            # default 0 and let the handler chain populate it.
            from .budget import StepContext as _StepContext
            ctx = _StepContext(
                state=self.budget_state,
                messages=messages,
                all_tools=tools_schema,
                current_tools=list(tools_schema),
                event_bus=self.event_bus,
                agent_id=self.agent_id,
                agent_name=self.config.name,
            )
            self.budget_tracker.apply_handlers(ctx)
            current_tools_schema = ctx.current_tools

            # Get current LLM params (may have been adjusted by parent)
            with self._params_lock:
                step_params = dict(self.llm_params)

            step_model = step_params.pop("model", self.model)
            step_tool_choice = step_params.pop("tool_choice", "required")

            # Stream callback
            _agent_name = self.config.name  # capture for closure
            def _on_token(tn: str, args: str, tok: int) -> None:
                self.event_bus.emit(EventType.AGENT_STREAM,
                                   agent_id=self.agent_id, agent_name=_agent_name,
                                   step=step, tool_name=tn, args_preview=args[:80], tok=tok)

            # Emit LLM request for tracing
            self.event_bus.emit(EventType.AGENT_LLM_REQUEST,
                               agent_id=self.agent_id, agent_name=self.config.name,
                               step=step,
                               messages=messages,
                               tools=current_tools_schema,
                               llm_params={"model": step_model, **step_params})

            try:
                response = self._observe_llm_call(
                    step=step, messages=messages, tools=current_tools_schema,
                    do_call=lambda: stream_llm(
                        self.llm, step_model, messages, current_tools_schema,
                        tool_choice=step_tool_choice, on_token=_on_token,
                        **step_params,
                    ),
                    mode="react",
                )
            except Exception as exc:
                log.error("agent '%s' step %d LLM call failed: %s: %s",
                          self.config.name, step, type(exc).__name__, exc)
                log.debug("LLM params: model=%s url=%s", step_model,
                          getattr(self.llm, '_base_url', getattr(self.llm, 'base_url', '?')))
                # Surface the LLM error on the agent.<name> span so
                # /qa/traces shows ✗ failed (otherwise the agent ends
                # quietly with no findings and looks like 'completed').
                try:
                    from opentelemetry import trace as _otel_trace
                    from opentelemetry.trace import StatusCode, Status as _Status
                    _cur = _otel_trace.get_current_span()
                    if _cur is not None:
                        _cur.record_exception(exc)
                        _cur.set_status(_Status(StatusCode.ERROR,
                                                 f"{type(exc).__name__}: {exc}"[:300]))
                except Exception:
                    pass
                # Also emit on the event bus so the events table — and
                # therefore /qa/sessions — gets a terminal marker. The
                # OTel span captures the failure for /qa/traces, but
                # the events stream is the diagram builder's source of
                # truth; without this it just stops at agent_llm_request.
                try:
                    self.event_bus.emit(
                        EventType.AGENT_LLM_ERROR,
                        agent_id=self.agent_id,
                        agent_name=self.config.name,
                        step=step,
                        error_class=type(exc).__name__,
                        error_message=f"{exc}"[:1000],
                        model=step_model,
                    )
                except Exception:
                    pass
                break

            # Update budget
            tok_in = tok_out = tok_cached = 0
            if response.usage:
                tok_in = response.usage.prompt_tokens
                tok_out = response.usage.completion_tokens
                tok_cached = _extract_cached(response.usage)
                self.budget_tracker.update_tokens(
                    self.budget_state,
                    total_tokens=response.usage.total_tokens,
                    tokens_in=tok_in, tokens_out=tok_out, tokens_cached=tok_cached,
                )
            self.budget_state.steps_used = step + 1

            msg = response.choices[0].message

            # Emit LLM response for tracing
            resp_tool_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    resp_tool_calls.append({
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    })
            uncached = max(0, tok_in - tok_cached)
            paid = uncached + int(tok_cached * self.budget_state.cache_discount) + tok_out
            self.event_bus.emit(EventType.AGENT_LLM_RESPONSE,
                               agent_id=self.agent_id, agent_name=self.config.name,
                               step=step,
                               tool_calls=resp_tool_calls,
                               content=msg.content or "",
                               usage={"prompt_tokens": tok_in,
                                      "completion_tokens": tok_out,
                                      "cached_tokens": tok_cached,
                                      "paid": paid})
            try:
                _stash_res({"content": msg.content or "",
                            "tool_calls": resp_tool_calls,
                            "usage": {"prompt_tokens": tok_in,
                                      "completion_tokens": tok_out,
                                      "cached_tokens": tok_cached,
                                      "paid": paid}})
            except Exception:
                pass

            # Classify tool calls
            tool_names = []
            dispatch_tcs = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_names.append(tc.function.name)
                    _called_tools.add(tc.function.name)
                    if tc.function.name in ("done", "reflect", "agent_spawn"):
                        continue
                    # Mocked tools take the sequential _handle_tool_call
                    # path so their real handler never runs (would waste
                    # work and could have side effects the test doesn't
                    # want).
                    if self.tool_mocks is not None and self.tool_mocks.has(tc.function.name):
                        continue
                    dispatch_tcs.append(tc)

            # Emit AGENT_STEP for every LLM round — even text-only (WAL: before dispatch)
            text_preview = ""
            if not tool_names and msg.content:
                text_preview = msg.content[:100].replace("\n", " ")
            self.event_bus.emit(EventType.AGENT_STEP,
                               agent_id=self.agent_id, agent_name=self.config.name,
                               step=step,
                               tool=", ".join(tool_names) if tool_names else "(text)",
                               tools=tool_names,
                               text_preview=text_preview,
                               args={},
                               tok_in=self.budget_state.tokens_in,
                               tok_out=self.budget_state.tokens_out,
                               tok_cached=self.budget_state.tokens_cached)

            if not msg.tool_calls:
                # Re-prompt sequence when the model emits text without any
                # tool call:
                #   1) text_response guard — generic "you must call a tool"
                #   2) require_tool:<name> guards — fire if a mandatory tool
                #      (typically `done`) was never called. Without this the
                #      agent natural-stops with output=None, the caller sees
                #      no findings, and side-effects already produced
                #      (e.g. reply_to_comment) silently get duplicated when
                #      the LLM is later asked to "call a tool" again.
                guard_msg = self._check_guard("text_response", _guard_retries, _MAX_GUARD_RETRIES)
                if guard_msg is None:
                    guard_msg = self._check_require_tool_guards(_called_tools, _guard_retries, _MAX_GUARD_RETRIES)
                if guard_msg:
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    messages.append({"role": "user", "content": guard_msg})
                    continue
                self._natural_stop = True
                break

            # Parallel dispatch of domain tools
            dispatch_results: dict[str, Any] = {}
            if dispatch_tcs:
                dispatch_results = self._dispatch_tools(dispatch_tcs)

            # Build message history
            messages.append({
                "role": "assistant",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })

            # Process each tool result
            step_record = StepRecord(step=step, llm_params=step_params)
            findings_from_done = None
            # Outcomes collected per tool call for handler post-dispatch
            # update (see ReflectCadenceCounter.on_step_done).
            step_outcomes: list[tuple[str, bool]] = []

            for tool_seq, tc in enumerate(msg.tool_calls, start=1):
                try:
                    tc_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tc_args = {}
                # Tool API boundary — request side. Captured before dispatch
                # so a crash inside the tool still leaves the request on disk.
                self.event_bus.emit(EventType.AGENT_TOOL_REQUEST,
                                   agent_id=self.agent_id, agent_name=self.config.name,
                                   step=step, seq=tool_seq,
                                   tool=tc.function.name,
                                   tool_call_id=tc.id,
                                   args=tc_args)
                # Same observable wrapper used for LLM calls — keeps
                # the trace shape uniform; AI debugger drills into
                # tool spans the exact same way it drills into LLM
                # spans (span_id → payload files).
                try:
                    from orchestra.otel import observe as _observe
                    from orchestra.otel_fs import (
                        stash_request as _stash_req,
                        stash_response as _stash_res,
                    )
                    _tool_ctx = _observe(f"tool.{tc.function.name}", {
                        "tool.name":         tc.function.name,
                        "tool.call_id":      tc.id,
                        "diffgraph.agent_id": self.agent_id,
                        "diffgraph.agent_name": self.config.name,
                        "diffgraph.step":     step,
                        "diffgraph.seq":      tool_seq,
                    })
                except Exception:
                    _tool_ctx = None
                    _stash_req = _stash_res = lambda *a, **kw: None

                if _tool_ctx is not None:
                    with _tool_ctx:
                        _stash_req({"name": tc.function.name,
                                    "args": tc_args,
                                    "tool_call_id": tc.id})
                        content = self._handle_tool_call(tc, dispatch_results, step)
                        clean_content = _sanitize_tool_content(content)
                        _stash_res({"content": clean_content,
                                    "content_len": len(clean_content)})
                else:
                    content = self._handle_tool_call(tc, dispatch_results, step)
                    clean_content = _sanitize_tool_content(content)
                self.event_bus.emit(EventType.AGENT_TOOL_RESPONSE,
                                   agent_id=self.agent_id, agent_name=self.config.name,
                                   step=step, seq=tool_seq,
                                   tool=tc.function.name,
                                   tool_call_id=tc.id,
                                   content=clean_content,
                                   content_len=len(clean_content))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": clean_content,
                })

                # Per-tool outcome — read by handler `on_step_done`
                # hooks (e.g. ReflectCadenceCounter decides whether to
                # reset the counter based on `reflect` outcomes).
                step_outcomes.append((
                    tc.function.name,
                    isinstance(content, str) and content.startswith("validation error"),
                ))

                # AGENT_TOOL_RESULT for EVERY tool — uniform shape.
                # Previously reflect and done were carved out as
                # special cases; now they flow through the same event
                # path as any domain tool. Trace UIs and CLI loggers
                # get one schema to render against.
                result_text = content
                result_preview = result_text[:200].replace("\n", " ").strip() if isinstance(result_text, str) else ""
                try:
                    tc_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tc_args = {}
                if isinstance(result_text, list):
                    r_count = len(result_text)
                elif isinstance(result_text, str) and "\n" in result_text:
                    r_count = result_text.count("\n") + 1
                else:
                    r_count = None
                self.event_bus.emit(EventType.AGENT_TOOL_RESULT,
                                   agent_id=self.agent_id, agent_name=self.config.name,
                                   step=step, tool=tc.function.name,
                                   args=tc_args,
                                   result_len=len(result_text) if isinstance(result_text, (str, list)) else 0,
                                   result_preview=result_preview,
                                   result_count=r_count)
                step_record.tool_calls.append({"name": tc.function.name})

                # `done` is the one tool whose result drives control
                # flow at the agent-loop level (terminates the run).
                # Validation error → don't mark done, let the LLM
                # retry on the next turn.
                if tc.function.name == "done":
                    if not (isinstance(content, str) and content.startswith("validation error")):
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        self._done_output = args.get("findings", args)
                        self._done_called = True
                        findings_from_done = self._done_output

            # Phase 2 of the pusher pipeline — let stateful handlers
            # (ReflectCadenceCounter, future ones) update themselves
            # based on what tools actually ran this step.
            ctx.step_outcomes = step_outcomes
            self.budget_tracker.notify_step_done(ctx)

            if response.usage:
                step_record.tokens_in = response.usage.prompt_tokens
                step_record.tokens_out = response.usage.completion_tokens
                step_record.tokens_cached = _extract_cached(response.usage)
            trace.steps.append(step_record)

            if findings_from_done is not None:
                # Check require_tool guards before finishing
                rt_msg = self._check_require_tool_guards(_called_tools, _guard_retries, _MAX_GUARD_RETRIES)
                if rt_msg:
                    messages.append({"role": "user", "content": rt_msg})
                    findings_from_done = None
                    self._done_called = False
                    self._done_output = None
                    continue

                trace.total_tokens = self.budget_state.tokens_used
                trace.total_steps = self.budget_state.steps_used
                self.event_bus.emit(EventType.AGENT_DONE,
                                   agent_id=self.agent_id, agent_name=self.config.name,
                                   output=findings_from_done,
                                   tok_in=self.budget_state.tokens_in,
                                   tok_out=self.budget_state.tokens_out,
                                   tok_cached=self.budget_state.tokens_cached)
                return AgentResult(
                    agent_id=self.agent_id, agent_name=self.config.name,
                    output=findings_from_done,
                    sgr_history=self.sgr.history if self.sgr else [],
                    messages=messages, budget_state=self.budget_state, trace=trace,
                )

            # Maybe condense
            messages = self._maybe_condense(messages)

        # Post-loop: natural stop (LLM returned text) or forced (budget/step limit)
        if getattr(self, '_natural_stop', False):
            self.event_bus.emit(EventType.AGENT_DONE,
                               agent_id=self.agent_id, agent_name=self.config.name)
            trace.total_tokens = self.budget_state.tokens_used
            trace.total_steps = self.budget_state.steps_used
            return AgentResult(
                agent_id=self.agent_id, agent_name=self.config.name,
                output=None,
                sgr_history=self.sgr.history if self.sgr else [],
                messages=messages, budget_state=self.budget_state, trace=trace,
            )

        self.event_bus.emit(EventType.AGENT_FORCED_DONE,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           reason="step limit" if not self.budget_state.exhausted else "token limit",
                           tok_in=self.budget_state.tokens_in,
                           tok_out=self.budget_state.tokens_out,
                           tok_cached=self.budget_state.tokens_cached)
        forced = self._force_done(messages, tools_schema)
        trace.total_tokens = self.budget_state.tokens_used
        trace.total_steps = self.budget_state.steps_used
        return AgentResult(
            agent_id=self.agent_id, agent_name=self.config.name,
            output=forced,
            sgr_history=self.sgr.history if self.sgr else [],
            messages=messages, budget_state=self.budget_state, trace=trace,
        )

    # ── Free-form artifact dump ───────────────────────────────────────────────

    def dump_artifact(self, name: str, data: Any) -> None:
        """
        Drop arbitrary JSON-serialisable data into the trace under this agent.

        Picked up by trace sinks (FS writer creates artifacts/<name>.json,
        DB writer stores it as an event row). No-op when no event bus is
        attached, so safe to call from anywhere.
        """
        self.event_bus.emit(EventType.AGENT_ARTIFACT,
                            agent_id=self.agent_id,
                            agent_name=self.config.name,
                            name=name, data=data)

    # ── Tool call handling ────────────────────────────────────────────────────

    def _handle_tool_call(self, tc, dispatch_results: dict, step: int) -> str:
        """Route all tool calls through registry.dispatch() — validation + handler.

        Mock interception lives here so it covers ANY tool, not just
        `agent_spawn`. A configured tool with a matching `when:` returns
        the canned response synchronously; no real handler runs.
        Configured tool but no matching entry → MissingMockMatchError
        (test author left a fixture hole — surface immediately).
        Tool not in the fixture → real dispatch (partial mocking).
        """
        name = tc.function.name
        # Plain parse — no preemptive repair. Both qwen3 failure
        # shapes (truncated keys / stringified args) are recovered
        # by the registry's `arg_repair_handlers` chain, which only
        # fires when schema validation actually flags a missing
        # required field. `raw_args` is forwarded so syntax-level
        # handlers can re-attempt parse on the original string.
        raw_args = tc.function.arguments
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = {}

        # ── Mock interception (Mockito-style, ordinal) ────────────────────
        if self.tool_mocks is not None and self.tool_mocks.has(name):
            from .tool_mocks import render_mock_result
            # consume() is thread-safe; raises MockExhaustedError /
            # MockArgsMismatchError if the agent's behaviour diverged
            # from what the fixture expects at this ordinal slot.
            entry = self.tool_mocks.consume(name, args)
            result = render_mock_result(name, entry, args)
            self.event_bus.emit(
                EventType.AGENT_TOOL_RESULT,
                agent_id=self.agent_id, agent_name=self.config.name,
                step=step, tool=name, args=args,
                result=str(result)[:1000], mocked=True,
                mock_when=entry.when,
            )
            return self.registry.format_result(name, result)

        # Pre-dispatched domain tools (ran in parallel earlier)
        if tc.id in dispatch_results:
            return self.registry.format_result(name, dispatch_results[tc.id])

        # Everything else through registry (builtins + domain, with schema validation).
        # raw_args lets the chain's syntax-level handlers (e.g.
        # TruncatedJsonHandler) re-attempt parse from the original
        # string when our fallback turned `args` into {}.
        result = self.registry.dispatch(name, args, raw_args=raw_args)
        return self.registry.format_result(name, result)

    # ── Meta-tool implementations ─────────────────────────────────────────────

    def _resolve_agent_config(self, agent_name: str) -> Optional[AgentConfig]:
        """Resolve agent config from registry (compiled prompts) or agent_configs dict."""
        # Try compiled registry first
        if self.agent_registry:
            entry = self.agent_registry.get(agent_name)
            if entry:
                return entry.to_agent_config()
        # Fallback to dict
        return self.agent_configs.get(agent_name)

    def _check_require_tool_guards(self, called_tools: set, retries: dict, max_retries: int) -> str | None:
        """Check all require_tool:* guards. Returns first unmet guard message or None."""
        guards = self.config.guards or {}
        for key, msg in guards.items():
            if not key.startswith("require_tool:"):
                continue
            tool_name = key.split(":", 1)[1]
            if tool_name in called_tools:
                continue
            guard_key = f"require_tool:{tool_name}"
            count = retries.get(guard_key, 0)
            if count >= max_retries:
                continue
            retries[guard_key] = count + 1
            try:
                msg = msg.format(**self.data_scope)
            except (KeyError, IndexError):
                pass
            log.info("guard 'require_tool:%s' fired for agent '%s' (retry %d/%d)",
                     tool_name, self.config.name, count + 1, max_retries)
            return msg
        return None

    def _check_guard(self, trigger: str, retries: dict, max_retries: int) -> str | None:
        """Check if a guard is configured and has retries left. Returns message or None."""
        guards = self.config.guards or {}
        if trigger not in guards:
            return None
        count = retries.get(trigger, 0)
        if count >= max_retries:
            return None
        retries[trigger] = count + 1
        # Interpolate {placeholders} from data_scope
        msg = guards[trigger]
        try:
            msg = msg.format(**self.data_scope)
        except (KeyError, IndexError):
            pass
        log.info("guard '%s' fired for agent '%s' (retry %d/%d)",
                 trigger, self.config.name, count + 1, max_retries)
        return msg

    def _resolve_data_inheritance(self, data: dict) -> dict:
        """Resolve explicit data values from spawn call."""
        resolved = {}
        for key, val in data.items():
            resolved[key] = str(val)
        return resolved

    def _meta_agent_spawn(self, args: dict) -> str:
        """agent_spawn: create and run a child agent with data injection.

        Mock interception happens earlier (in `_handle_tool_call`) since
        agent_spawn is a normal tool from the registry's perspective.
        By the time this method runs the call is real.
        """
        agent_name = args.get("agent", "")
        focus_arg = args.get("focus", "")

        agent_config = self._resolve_agent_config(agent_name)
        if not agent_config:
            return json.dumps({"error": f"unknown agent: {agent_name}"})
        if self.depth >= self.config.max_depth:
            return json.dumps({"error": "max depth reached"})

        # Data: inherit parent scope, merge explicit, auto-resolve from:tool.field
        data = args.get("data", {})
        resolved_data = dict(self.data_scope)
        if data:
            resolved_data.update(self._resolve_data_inheritance(data))
        if focus_arg and "focus" not in resolved_data:
            resolved_data["focus"] = focus_arg

        # Templates pull lazy data via the `{{ pr.* }}` proxy on
        # RunContext.registry (see orchestra/runcontext.py
        # _HiddenToolProxy). No pre-resolution step needed.
        resolved_data = dict(resolved_data)

        # Pass handoff context if explicitly requested by LLM
        context: list[dict] = []
        handoff_mode = args.get("context_handoff", "")
        if handoff_mode:
            from .handoff import get_handoff
            handoff = get_handoff(handoff_mode)
            context = handoff.apply(
                [], self.sgr.history if self.sgr else [], None, self.llm, self.model
            )

        # Child uses its own budget from .prompt config — not overridden by parent
        child_config = agent_config

        # Child gets own registry (inherits domain tools, fresh builtins)
        from .tools.builtin import register_builtins
        child_registry = self.registry.clone()
        child = Agent(
            config=child_config, tool_registry=child_registry,
            llm=self.llm, model=self.model, event_bus=self.event_bus,
            parent_id=self.agent_id, parent=self, depth=self.depth + 1,
            context_messages=context, agent_configs=self.agent_configs, agent_registry=self.agent_registry,
        )
        register_builtins(child_registry, child_config, sgr_tracker=child.sgr, agent=child)
        child.data_scope = resolved_data  # for further inheritance

        with self._children_lock:
            self._children[child.agent_id] = child

        # Log spawn details
        spawn_focus = args.get("focus", "")
        data_keys = list(resolved_data.keys()) if resolved_data else []
        self.event_bus.emit(EventType.AGENT_SPAWNED,
                           parent_id=self.agent_id, child_id=child.agent_id,
                           agent_name=agent_name, focus=spawn_focus,
                           data_keys=data_keys)

        wait = args.get("wait", True)
        if wait:
            # No span here — Agent.run() emits its own. Same path
            # for root and spawned children, no duplication.
            result = child.run()
            with self._children_lock:
                self._children_results[child.agent_id] = result
            if child.budget_state:
                self.budget_tracker.debit_child(self.budget_state, child.budget_state)
            # Return output + SGR summary for parent to consolidate
            sgr_summary = ""
            if result.sgr_history:
                last = result.sgr_history[-1]
                sgr_summary = f"confidence={last.confidence}, learned: {last.learned[:300]}"
            return json.dumps({
                "status": "completed",
                "agent_id": child.agent_id,
                "agent_name": agent_name,
                "output": result.output,
                "sgr_summary": sgr_summary,
                "steps": child.budget_state.steps_used if child.budget_state else 0,
                "tokens": child.budget_state.tokens_used if child.budget_state else 0,
            }, ensure_ascii=False, indent=2, default=str)
        else:
            # Async — run in background thread
            def _run():
                result = child.run()
                with self._children_lock:
                    self._children_results[child.agent_id] = result
                if child.budget_state:
                    self.budget_tracker.debit_child(self.budget_state, child.budget_state)

            threading.Thread(target=_run, daemon=True).start()
            return json.dumps({"status": "spawned", "agent_id": child.agent_id})

    def _meta_budget_stats(self, args: dict) -> str:
        """budget_stats: surface this agent's current consumption +
        rough cost-of-spawn estimates + per-child consumption so the
        prompt can plan spawn-vs-direct trade-offs.

        Returned as a plain text summary in:
          - "your own session" — context (per-agent LLM window)
          - "shared with children" — tokens + steps (carved per spawn)
          - "typical investigator spawn" — hardcoded rough estimates
          - "subagents" — per-child name / focus / status / consumption
            (only when this agent has actually spawned children)

        Pure state — no instruction to the agent. Reaction logic
        lives in the prompt (see docs/orchestra-architecture.md
        §Tool-result convention)."""
        from .budget_stats import format_budget_stats
        if self.budget_state is None:
            return "(no budget state — agent has not started)"
        # Snapshot child state under the lock — children may still be
        # mutating their own budget_state if any were spawned async.
        children_snapshot = []
        with self._children_lock:
            for cid, child in self._children.items():
                completed = cid in self._children_results
                bs = child.budget_state
                children_snapshot.append({
                    "name": child.config.name,
                    "focus": (child.data_scope or {}).get("focus", ""),
                    "status": "completed" if completed else "running",
                    "steps_used": bs.steps_used if bs else 0,
                    "tokens_in": bs.tokens_in if bs else 0,
                    "cumulative_paid": bs.cumulative_paid if bs else 0,
                })
        return format_budget_stats(self.budget_state, children=children_snapshot)

    def _meta_agent_list(self, args: dict) -> str:
        """agent_list: return the agent registry for discovery."""
        if self.agent_registry:
            return json.dumps(self.agent_registry.to_listing(), indent=2, ensure_ascii=False)
        # Fallback: list from agent_configs dict
        listing = []
        for name, cfg in self.agent_configs.items():
            listing.append({
                "name": name,
                "summary": cfg.system_prompt[:200] if cfg.system_prompt else "",
                "mode": cfg.mode.value if hasattr(cfg.mode, 'value') else str(cfg.mode),
                "tools": cfg.tools,
            })
        return json.dumps(listing, indent=2, ensure_ascii=False)

    # ── Message building ──────────────────────────────────────────────────────

    def _build_messages(self) -> list[dict]:
        # Build one RunContext per message-build pass, reused for
        # both system and user message renders. Both go through
        # the Jinja engine (orchestra/template_engine.py).
        system_text = load_prompt(self.config.system_prompt)
        from .runcontext import RunContext
        from .template_engine import render as _render_template
        _ctx = RunContext(
            data={**(self.data_scope or {}), **(self.prompt_vars or {})},
            agent_name=self.config.name,
            agent_id=self.agent_id,
            depth=self.depth,
            budget_state=self.budget_state,
            skills_body=getattr(self, "_mounted_skills_body", "") or "",
            reflect=dict(self.config.reflect or {}),
            registry=self.registry,
        )
        if system_text:
            system_text = _render_template(system_text, _ctx)
        messages = [{"role": "system", "content": system_text}]
        messages.extend(self.context_messages)

        # User message — the body of whichever source we parsed in
        # __init__ (override > config.user_prompt). Frontmatter is
        # already stripped; what reaches the LLM is the body
        # rendered against the same RunContext used for system_text.
        user_text = self._fm_body
        if user_text:
            user_text = _render_template(user_text, _ctx)

        # Some endpoints reject requests without a user-role message;
        # this is a defensive fallback that never fires for our agents
        # (all of them have a user.md). Kept inline because there's
        # nothing to externalise — by definition the LLM never reads
        # it during real runs.
        if not any(m.get("role") == "user" for m in messages):
            messages.append({"role": "user", "content": user_text.strip() or "Begin."})
        return messages

    def _build_tool_names(self) -> list[str]:
        # Single source of truth: AgentConfig.tools is the agent's
        # default toolset. User-message frontmatter can subset it
        # (declared `tools:`) and/or pivot to meta-dispatch
        # (`dispatch_mode: meta`).
        default_names = [t for t in self.config.tools if self.registry.has(t)]

        # `extra_tools` from frontmatter are already registered in
        # __init__ — include them in the candidate pool so frontmatter
        # `tools:` can reference them.
        extra_names = [
            spec["name"] for spec in self._fm_meta.get("extra_tools", []) or []
            if self.registry.has(spec["name"])
        ]
        candidate_pool = set(default_names) | set(extra_names)

        fm_tools = self._fm_meta.get("tools")
        if isinstance(fm_tools, list):
            # Additive: defaults + user-prompt tools + skill tools
            # (mount_skills already merged skills' tools into
            # _fm_meta["tools"]). Always union, never replace —
            # the user layer can only EXTEND the agent's base
            # toolset.
            unknown = [
                n for n in fm_tools
                if n not in candidate_pool and not self.registry.has(n)
            ]
            if unknown:
                raise ValueError(
                    f"frontmatter.tools references names not in agent's "
                    f"@tools or extra_tools and not in the registry: "
                    f"{unknown}. Either declare them in extra_tools or "
                    f"verify the name."
                )
            names = list(default_names)
            for n in fm_tools:
                if n not in names:
                    names.append(n)
        else:
            names = default_names

        # Hide agent_spawn at max depth so we don't recurse infinitely.
        if self.depth >= self.config.max_depth:
            names = [t for t in names if t != "agent_spawn"]

        # Meta dispatch — replace the LLM-visible schema with the two
        # meta-tools (list_tools / call_tool), which themselves expose
        # the `names` subset internally. The LLM sees a stable schema
        # regardless of the subset; the registry still has the real
        # tools for call_tool to dispatch to.
        if self._fm_meta.get("dispatch_mode") == "meta":
            from .tools.meta import build_meta_tools
            list_td, call_td = build_meta_tools(self.registry, allowed=set(names))
            self.registry.register_tool_def(list_td)
            self.registry.register_tool_def(call_td)
            return ["list_tools", "call_tool"]
        return names

    def _drain_injected(self, messages: list[dict]) -> None:
        """Drain queued injected messages into the message list."""
        with self._inject_lock:
            for msg in self._injected_messages:
                messages.append({"role": "user", "content": msg})
            self._injected_messages.clear()

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch_tools(self, tool_calls: list) -> dict[str, Any]:
        results: dict[str, Any] = {}
        if not tool_calls:
            return results

        def _run(tc):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return tc.id, self.registry.dispatch(tc.function.name, args)

        with ThreadPoolExecutor(max_workers=max(1, len(tool_calls))) as executor:
            futures = {executor.submit(_run, tc): tc for tc in tool_calls}
            for future in as_completed(futures):
                try:
                    tc_id, result = future.result()
                    results[tc_id] = result
                except Exception as e:
                    tc = futures[future]
                    results[tc.id] = f"error: {e}"
        return results

    # ── Condensation ──────────────────────────────────────────────────────────

    def _maybe_condense(self, messages: list[dict]) -> list[dict]:
        cfg = self.config.condensation
        if not cfg or not cfg.enabled:
            return messages
        if not should_condense(messages, cfg.trigger):
            return messages
        condenser = get_condenser(cfg.strategy)
        sgr_entries = self.sgr.history if self.sgr else []
        self.event_bus.emit(EventType.CONDENSATION_TRIGGERED,
                           agent_id=self.agent_id, strategy=cfg.strategy.value,
                           message_count=len(messages))
        return condenser.condense(messages, cfg, sgr_entries, self.llm, self.model)

    # ── Force done ────────────────────────────────────────────────────────────

    def _force_done(self, messages: list[dict], tools_schema: list[dict]) -> Any:
        messages.append({
            "role": "user",
            "content": load_internal("pushers/step_limit"),
        })
        done_tools = [t for t in tools_schema if t["function"]["name"] == "done"]
        if not done_tools:
            return None
        lp = self.config.llm_params or LLMParamsConfig()
        try:
            response = stream_llm(self.llm, self.model, messages, done_tools,
                                  tool_choice=lp.tool_choice)
            msg = response.choices[0].message
            if msg.tool_calls:
                args = json.loads(msg.tool_calls[0].function.arguments or "{}")
                return args.get("findings", args)
        except Exception as exc:
            log.warning("force done failed: %s", exc)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_parse_json(text: str) -> Any:
    content = text.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content


def _extract_cached(usage: Any) -> int:
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return cached
    return getattr(usage, "prompt_cache_hit_tokens", 0) or 0


def _stamp_usage(span: Any, response: Any) -> None:
    """Stamp prompt/completion/cached token counts on the open
    llm.request span so /qa/traces can aggregate per-row tokens
    without parsing FS payloads. No-op if usage is missing or the
    span doesn't accept attributes (defensive — tracing must never
    crash the agent loop)."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None or span is None:
            return
        tin = int(getattr(usage, "prompt_tokens", 0) or 0)
        tout = int(getattr(usage, "completion_tokens", 0) or 0)
        tcache = int(_extract_cached(usage) or 0)
        span.set_attribute("llm.tokens_in", tin)
        span.set_attribute("llm.tokens_out", tout)
        span.set_attribute("llm.tokens_cached", tcache)
        # Convenience: tokens_in_no_cache = prompt_tokens - cached
        span.set_attribute("llm.tokens_in_uncached", max(0, tin - tcache))
    except Exception:
        pass
