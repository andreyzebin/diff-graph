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

from .types import AgentConfig, AgentMode, BudgetConfig, LLMParamsConfig, PusherType
from .budget import BudgetTracker, BudgetState, PusherAction
from .events import EventBus, EventType
from .sgr import SGRTracker, SGREntry
from .condensation import get_condenser, should_condense
from .streaming import stream_llm
from .prompts import load_prompt, interpolate, load_internal
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


def resolve_agent_data(config: AgentConfig, data: dict, registry: ToolRegistry) -> dict:
    """
    Resolve @data fields for an agent config. Single path for all agents.

    1. Start with provided data
    2. Auto-resolve from:tool.field for missing fields (cached data providers)
    3. Interpolate resolved data into system prompt

    Returns the resolved data dict. Mutates config.system_prompt in place.
    """
    resolved = dict(data)
    schema = getattr(config, "input_schema", None) or {}
    for field_name, field_meta in schema.items():
        if field_name in resolved and resolved[field_name] not in (
            "(not available)", "(not yet loaded)", ""
        ):
            continue
        from_tool = field_meta.get("from_tool")
        from_field = field_meta.get("from_field")
        if not from_tool or not from_field:
            continue
        if not registry.has(from_tool):
            continue
        try:
            result = registry.call_data_provider(from_tool)
            if isinstance(result, dict) and from_field in result:
                resolved[field_name] = str(result[from_field])
        except Exception as e:
            log.warning("from: tool '%s' failed for field '%s': %s", from_tool, field_name, e)
            resolved[field_name] = "(not available)"

    if resolved:
        # Interpolate BOTH system and user templates. After the
        # system/user split most per-call placeholders ({diff_summary},
        # {focus}, {message}, …) live in user_prompt now; system_prompt
        # is largely placeholder-free but the same data can be quoted
        # in either.
        if "{" in config.system_prompt:
            config.system_prompt = interpolate(config.system_prompt, **resolved)
        if config.user_prompt and "{" in config.user_prompt:
            config.user_prompt = interpolate(config.user_prompt, **resolved)

    return resolved


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
        self.data_scope: dict[str, str] = {}  # resolved @data values for inheritance

        # SGR
        self.sgr = SGRTracker(config.sgr_extensions) if "reflect" in config.tools else None

        # Budget
        self.budget_tracker = BudgetTracker(config.budget, self.event_bus)
        self.budget_state: Optional[BudgetState] = None

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
        (called from spawn_agent), so the trace tree is uniform.
        """
        self.event_bus.emit(EventType.AGENT_STARTED,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           parent_id=self.parent_id, depth=self.depth)
        try:
            from opentelemetry import trace as _otel_trace
            _tracer = _otel_trace.get_tracer("diffgraph-agent")
            _span_cm = _tracer.start_as_current_span(
                f"agent.{self.config.name}",
                attributes={
                    "diffgraph.agent_id":   self.agent_id,
                    "diffgraph.agent_name": self.config.name,
                    "diffgraph.depth":      self.depth,
                    "diffgraph.parent_id":  self.parent_id or "",
                    "diffgraph.mode":       self.config.mode.name,
                },
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

    def _run_single(self) -> AgentResult:
        messages = self._build_messages()
        try:
            from .streaming import _llm_call_with_retry
            response = _llm_call_with_retry(lambda: self.llm.chat.completions.create(
                model=self.llm_params.get("model", self.model),
                messages=messages,
                temperature=self.llm_params.get("temperature", 0),
                stream=False,
            ))
            content = (response.choices[0].message.content or "").strip()
            output = _try_parse_json(content)
        except Exception as exc:
            log.warning("single-shot agent '%s' failed: %s", self.config.name, exc)
            output = None

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
        steps_since_reflect = 0
        self._natural_stop = False
        _guard_retries: dict[str, int] = {}  # guard_name → retry count
        _MAX_GUARD_RETRIES = 2
        _called_tools: set[str] = set()  # track all tools called during this run

        for step in range(self.config.budget.max_steps):
            if self.budget_state.exhausted:
                self.event_bus.emit(EventType.AGENT_FORCED_DONE,
                                   agent_id=self.agent_id, agent_name=self.config.name, reason="token limit",
                                   tok_in=self.budget_state.tokens_in,
                                   tok_out=self.budget_state.tokens_out,
                                   tok_cached=self.budget_state.tokens_cached)
                break

            # Drain injected messages from parent
            self._drain_injected(messages)

            # Check pushers
            actions = self.budget_tracker.check_pushers(self.budget_state)
            current_tools_schema = tools_schema
            for action in actions:
                current_tools_schema = self._apply_pusher(
                    action, messages, tools_schema, current_tools_schema
                )

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

            # Single observable wrapper: same shape for LLM and tool
            # calls. Stashes full request body for the AI debugger
            # to drill into via the span_id-keyed payload file.
            try:
                from orchestra.otel import observe as _observe
                from orchestra.otel_fs import stash_request as _stash_req
                from orchestra.otel_fs import stash_response as _stash_res
                _llm_ctx = _observe("llm.request", {
                    "llm.model": step_model,
                    "llm.message_count": len(messages),
                    "llm.tools_count": len(current_tools_schema or []),
                    "diffgraph.agent_id": self.agent_id,
                    "diffgraph.agent_name": self.config.name,
                    "diffgraph.step": step,
                })
            except Exception:
                _llm_ctx = None
                _stash_req = _stash_res = lambda *a, **kw: None

            try:
                if _llm_ctx is not None:
                    with _llm_ctx:
                        _stash_req({"model": step_model,
                                    "messages": messages,
                                    "tools": current_tools_schema,
                                    "params": step_params})
                        response = stream_llm(
                            self.llm, step_model, messages, current_tools_schema,
                            tool_choice=step_tool_choice, on_token=_on_token,
                            **step_params,
                        )
                else:
                    response = stream_llm(
                        self.llm, step_model, messages, current_tools_schema,
                        tool_choice=step_tool_choice, on_token=_on_token,
                        **step_params,
                    )
            except Exception as exc:
                log.error("agent '%s' step %d LLM call failed: %s: %s",
                          self.config.name, step, type(exc).__name__, exc)
                log.debug("LLM params: model=%s url=%s", step_model,
                          getattr(self.llm, '_base_url', getattr(self.llm, 'base_url', '?')))
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
                    if tc.function.name in ("done", "reflect", "spawn_agent"):
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

            # Emit reflect events
            for tc in msg.tool_calls:
                if tc.function.name == "reflect":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if self.sgr:
                        self.sgr.record(step, args)
                        self.event_bus.emit(EventType.AGENT_REFLECT,
                                           agent_id=self.agent_id, agent_name=self.config.name,
                                           step=step, **args)
                        steps_since_reflect = 0
                elif tc.function.name != "done":
                    steps_since_reflect += 1

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

                if tc.function.name == "done":
                    # Trust registry.dispatch's JSON-Schema validation. If
                    # `content` came back as a "validation error: ..." string,
                    # the args didn't match `done`'s schema (e.g. some models
                    # send `findings` as a string instead of an array). Don't
                    # mark done — the error already lives in the tool result,
                    # so the next LLM round will see it and retry.
                    if not (isinstance(content, str) and content.startswith("validation error")):
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        self._done_output = args.get("findings", args)
                        self._done_called = True
                        findings_from_done = self._done_output
                elif tc.function.name != "reflect":
                    # Emit AGENT_TOOL_RESULT for ALL tools (domain + meta)
                    result_text = content
                    # Build a short preview of the result
                    result_preview = result_text[:200].replace("\n", " ").strip()
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
                                       result_len=len(result_text),
                                       result_preview=result_preview,
                                       result_count=r_count)
                    step_record.tool_calls.append({"name": tc.function.name})

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
        `spawn_agent`. A configured tool with a matching `when:` returns
        the canned response synchronously; no real handler runs.
        Configured tool but no matching entry → MissingMockMatchError
        (test author left a fixture hole — surface immediately).
        Tool not in the fixture → real dispatch (partial mocking).
        """
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
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

        # Everything else through registry (builtins + domain, with schema validation)
        result = self.registry.dispatch(name, args)
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

    def _meta_spawn_agent(self, args: dict) -> str:
        """spawn_agent: create and run a child agent with data injection.

        Mock interception happens earlier (in `_handle_tool_call`) since
        spawn_agent is a normal tool from the registry's perspective.
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

        # Single path: resolve from: providers + interpolate prompt
        resolved_data = resolve_agent_data(agent_config, resolved_data, self.registry)

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

    def _meta_list_agents(self, args: dict) -> str:
        """list_agents: return the agent registry for discovery."""
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
        # System: stable methodology, ideally NO per-call placeholders
        # (so the LLM's prompt cache is reusable across runs). During
        # migration prompt files may still carry placeholders here —
        # interpolation runs but should resolve to a stable string.
        system_text = load_prompt(self.config.system_prompt)
        if self.prompt_vars:
            system_text = interpolate(system_text, **self.prompt_vars)
        messages = [{"role": "system", "content": system_text}]
        messages.extend(self.context_messages)

        # User: per-call template. user_message_override (set by a
        # parent spawn or a unit-test override) wins over the agent's
        # default user_prompt template.
        user_text = ""
        if self.user_message_override is not None:
            user_text = self.user_message_override
        elif self.config.user_prompt:
            user_text = self.config.user_prompt
            if self.prompt_vars:
                user_text = interpolate(user_text, **self.prompt_vars)

        # Some endpoints reject requests without a user-role message;
        # this is a defensive fallback that never fires for our agents
        # (all of them have a user.md). Kept inline because there's
        # nothing to externalise — by definition the LLM never reads
        # it during real runs.
        if not any(m.get("role") == "user" for m in messages):
            messages.append({"role": "user", "content": user_text.strip() or "Begin."})
        return messages

    def _build_tool_names(self) -> list[str]:
        # Single source of truth: AgentConfig.tools holds every tool the
        # agent should see. The registry knows which handler to call for
        # each name (domain closure vs framework meta).
        names = [t for t in self.config.tools if self.registry.has(t)]
        # Hide spawn_agent at max depth so we don't recurse infinitely.
        if self.depth >= self.config.max_depth:
            names = [t for t in names if t != "spawn_agent"]
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

    # ── Budget pushers ────────────────────────────────────────────────────────

    def _apply_pusher(self, action: PusherAction, messages: list[dict],
                      all_tools: list[dict], current_tools: list[dict]) -> list[dict]:
        if action.type == PusherType.NUDGE:
            messages.append({"role": "user", "content": action.message})
            return current_tools
        elif action.type == PusherType.FORCE_REFLECT:
            return [t for t in all_tools if t["function"]["name"] == "reflect"]
        elif action.type == PusherType.FORCE_DONE:
            return [t for t in all_tools if t["function"]["name"] == "done"]
        elif action.type == PusherType.CUSTOM and action.handler:
            try:
                action.handler(messages, self.budget_state)
            except Exception as e:
                log.warning("custom pusher failed: %s", e)
        return current_tools

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
