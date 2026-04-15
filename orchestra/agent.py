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
from .prompts import load_prompt, interpolate
from .tools.registry import ToolRegistry

log = logging.getLogger(__name__)


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

    if resolved and "{" in config.system_prompt:
        config.system_prompt = interpolate(config.system_prompt, **resolved)

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
        self.data_scope: dict[str, str] = {}  # resolved @data values for inheritance

        # SGR
        self.sgr = SGRTracker(config.sgr_extensions) if config.sgr else None

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
        return params

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> AgentResult:
        """Main entry point."""
        self.event_bus.emit(EventType.AGENT_STARTED,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           parent_id=self.parent_id, depth=self.depth)

        if self.config.mode == AgentMode.SINGLE:
            return self._run_single()
        return self._run_react()

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
            response = self.llm.chat.completions.create(
                model=self.llm_params.get("model", self.model),
                messages=messages,
                temperature=self.llm_params.get("temperature", 0),
                stream=False,
            )
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

            try:
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

            # Classify tool calls
            tool_names = []
            dispatch_tcs = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_names.append(tc.function.name)
                    if tc.function.name not in ("done", "reflect", "spawn_agent", "spawn_many",
                                                "plan", "fork", "adjust_agent", "observe_agents"):
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

            for tc in msg.tool_calls:
                content = self._handle_tool_call(tc, dispatch_results, step)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

                if tc.function.name == "done":
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

    # ── Tool call handling ────────────────────────────────────────────────────

    def _handle_tool_call(self, tc, dispatch_results: dict, step: int) -> str:
        """Route all tool calls through registry.dispatch() — validation + handler."""
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

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

    def _resolve_data_inheritance(self, data: dict) -> dict:
        """Resolve explicit data values from spawn call."""
        resolved = {}
        for key, val in data.items():
            resolved[key] = str(val)
        return resolved

    def _meta_spawn_agent(self, args: dict) -> str:
        """spawn_agent: create and run a child agent with data injection."""
        agent_name = args.get("agent", "")
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
        focus_arg = args.get("focus", "")
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

    def _meta_spawn_many(self, args: dict) -> str:
        """spawn_many: fan-out N agents in parallel, wait for all, return merged results."""
        agents_specs = args.get("agents", [])
        if not agents_specs:
            return json.dumps({"error": "no agents specified"})
        if self.depth >= self.config.max_depth:
            return json.dumps({"error": "max depth reached"})

        from .merge import get_merge_strategy

        handoff_mode = args.get("context_handoff", "")
        merge_name = args.get("merge", "union")

        results: list[AgentResult] = []

        def _run_one(spec: dict) -> AgentResult:
            agent_name = spec.get("agent", "")
            agent_config = self._resolve_agent_config(agent_name)
            if not agent_config:
                return AgentResult(agent_name=agent_name, output={"error": f"unknown: {agent_name}"})

            # Data inheritance: always inherit parent scope, merge explicit data
            spec_data = spec.get("data", {})
            resolved_data = dict(self.data_scope)  # start with parent's scope
            if spec_data:
                explicit = self._resolve_data_inheritance(spec_data)
                resolved_data.update(explicit)
            # Merge top-level focus
            focus_from_spec = spec.get("focus", "")
            if focus_from_spec and "focus" not in resolved_data:
                resolved_data["focus"] = focus_from_spec
            # Single path: resolve from: providers + interpolate prompt
            resolved_data = resolve_agent_data(agent_config, resolved_data, self.registry)

            # Pass handoff context if requested
            context: list[dict] = []
            if handoff_mode:
                from .handoff import get_handoff
                handoff = get_handoff(handoff_mode)
                context = handoff.apply(
                    [], self.sgr.history if self.sgr else [], None, self.llm, self.model
                )

            # Child uses its own budget from .prompt config
            child_config = agent_config

            from .tools.builtin import register_builtins
            child_registry = self.registry.clone()
            child = Agent(
                config=child_config, tool_registry=child_registry,
                llm=self.llm, model=self.model, event_bus=self.event_bus,
                parent_id=self.agent_id, parent=self, depth=self.depth + 1,
                context_messages=context, agent_configs=self.agent_configs, agent_registry=self.agent_registry,
            )
            register_builtins(child_registry, child_config, sgr_tracker=child.sgr, agent=child)
            child.data_scope = resolved_data
            with self._children_lock:
                self._children[child.agent_id] = child
            self.event_bus.emit(EventType.AGENT_SPAWNED,
                               parent_id=self.agent_id, child_id=child.agent_id,
                               agent_name=agent_name, focus=focus_from_spec,
                               data_keys=list(resolved_data.keys()) if resolved_data else [])
            r = child.run()
            if child.budget_state:
                self.budget_tracker.debit_child(self.budget_state, child.budget_state)
            return r

        with ThreadPoolExecutor(max_workers=len(agents_specs)) as executor:
            futures = {executor.submit(_run_one, s): s for s in agents_specs}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    log.warning("spawn_many child failed: %s", e)

        merge_strategy = get_merge_strategy(merge_name, llm=self.llm, model=self.model)
        merged = merge_strategy.merge(results)

        return json.dumps({
            "status": "completed",
            "agent_count": len(results),
            "merged_output": merged,
            "individual_outputs": [{"agent": r.agent_name, "output": r.output} for r in results],
        }, ensure_ascii=False, indent=2, default=str)

    def _meta_plan(self, args: dict) -> str:
        """plan: spawn a lightweight planner sub-agent."""
        from .tools.builtin import DEFAULT_PLAN_PROMPT

        goal = args.get("goal", "")
        constraints = args.get("constraints", "")
        output_hint = args.get("output_hint", "")

        user_content = f"Goal: {goal}"
        if constraints:
            user_content += f"\nConstraints: {constraints}"
        if output_hint:
            user_content += f"\nOutput format: {output_hint}"
        if self.sgr and self.sgr.last:
            user_content += f"\n\nCurrent knowledge:\n{self.sgr.last.learned[:500]}"
            if self.sgr.last.questions_remaining:
                user_content += "\nOpen questions:\n" + "\n".join(
                    f"- {q}" for q in self.sgr.last.questions_remaining[:5]
                )

        try:
            resp = self.llm.chat.completions.create(
                model=self.llm_params.get("model", self.model),
                messages=[
                    {"role": "system", "content": DEFAULT_PLAN_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
                stream=False,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return content
        except Exception as e:
            return json.dumps({"error": f"plan failed: {e}"})

    def _meta_fork(self, args: dict) -> str:
        """fork: clone self into N parallel branches with different focus."""
        branches = args.get("branches", [])
        if not branches:
            return json.dumps({"error": "no branches specified"})
        if self.depth >= self.config.max_depth:
            return json.dumps({"error": "max depth reached"})

        from .handoff import get_handoff
        from .merge import get_merge_strategy

        handoff_mode = args.get("context_handoff", "full_history")
        merge_name = args.get("merge", "best_confidence")
        n = min(len(branches), 4)

        results: list[AgentResult] = []

        def _run_branch(branch: dict) -> AgentResult:
            handoff = get_handoff(handoff_mode)
            context = handoff.apply(
                self.context_messages, self.sgr.history if self.sgr else [],
                None, self.llm, self.model
            )
            focus = branch.get("focus", "")
            if focus:
                context.append({"role": "user", "content": f"Focus: {focus}"})

            child_budget = self.budget_tracker.allocate_child(self.budget_state, 0.8 / n)
            child_config = AgentConfig(
                name=f"{self.config.name}_fork", system_prompt=self.config.system_prompt,
                mode=self.config.mode, sgr=self.config.sgr,
                tools=list(self.config.tools), meta_tools=[],  # forks don't get meta-tools
                output_schema=self.config.output_schema, budget=child_budget,
                llm_params=self.config.llm_params, max_depth=self.config.max_depth,
            )

            from .tools.builtin import register_builtins
            child_registry = self.registry.clone()
            child = Agent(
                config=child_config, tool_registry=child_registry,
                llm=self.llm, model=self.model, event_bus=self.event_bus,
                parent_id=self.agent_id, parent=self, depth=self.depth + 1,
                context_messages=context, agent_configs=self.agent_configs, agent_registry=self.agent_registry,
            )
            register_builtins(child_registry, child_config, sgr_tracker=child.sgr, agent=child)
            return child.run()

        with ThreadPoolExecutor(max_workers=n) as executor:
            futures = {executor.submit(_run_branch, b): b for b in branches[:n]}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    log.warning("fork branch failed: %s", e)

        merge_strategy = get_merge_strategy(merge_name, llm=self.llm, model=self.model)
        merged = merge_strategy.merge(results)

        return json.dumps({
            "status": "completed",
            "branch_count": len(results),
            "merged_output": merged,
        }, ensure_ascii=False, indent=2, default=str)

    def _meta_adjust_agent(self, args: dict) -> str:
        """adjust_agent: modify a child agent's LLM params, inject message, or adjust budget."""
        target_id = args.get("agent_id", "")
        with self._children_lock:
            child = self._children.get(target_id)
        if not child:
            return json.dumps({"error": f"agent {target_id} not found or not a child"})

        result: dict[str, Any] = {"agent_id": target_id}

        # Adjust params
        param_keys = ("temperature", "frequency_penalty", "presence_penalty",
                      "top_p", "max_completion_tokens", "model")
        param_changes = {k: args[k] for k in param_keys if k in args}
        if param_changes:
            applied = child.adjust_params(param_changes, source=self.agent_id)
            result["params_adjusted"] = applied

        # Inject message
        message = args.get("inject_message", "")
        if message:
            child.inject_message(message)
            result["message_injected"] = True

        # Budget adjustment
        extend_steps = args.get("extend_budget_steps", 0)
        if extend_steps and child.budget_state:
            max_delta = self.config.budget.max_feedback_budget_delta
            extend_steps = min(extend_steps, max_delta)
            child.budget_state.original_steps += extend_steps
            result["budget_extended_steps"] = extend_steps

        return json.dumps(result, indent=2, default=str)

    def _meta_observe_agents(self, args: dict) -> str:
        """observe_agents: return status of all child agents."""
        with self._children_lock:
            children = list(self._children.values())
        statuses = [c.get_status() for c in children]
        # Include results for completed children
        with self._children_lock:
            for status in statuses:
                aid = status["agent_id"]
                if aid in self._children_results:
                    r = self._children_results[aid]
                    status["output"] = r.output
                    status["status"] = "done"
        return json.dumps(statuses, indent=2, default=str)

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
                "meta_tools": cfg.meta_tools,
            })
        return json.dumps(listing, indent=2, ensure_ascii=False)

    # ── Message building ──────────────────────────────────────────────────────

    def _build_messages(self) -> list[dict]:
        prompt_text = load_prompt(self.config.system_prompt)
        if self.prompt_vars:
            prompt_text = interpolate(prompt_text, **self.prompt_vars)
        messages = [{"role": "system", "content": prompt_text}]
        messages.extend(self.context_messages)
        return messages

    def _build_tool_names(self) -> list[str]:
        names = list(self.config.tools)
        # Add meta-tools if not at max depth
        if self.depth < self.config.max_depth:
            for mt in self.config.meta_tools:
                if self.registry.has(mt) and mt not in names:
                    names.append(mt)
        # SGR
        if self.config.sgr and self.registry.has("reflect"):
            if "reflect" not in names:
                names.append("reflect")
        # Done
        if self.registry.has("done") and "done" not in names:
            names.append("done")
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
            "content": "Step limit reached. Call done() now with all findings you have so far.",
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
