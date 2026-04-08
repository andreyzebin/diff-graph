"""
Agent: the core execution unit.

Supports two modes:
  - single: one LLM call, no tools (strategist-style)
  - react:  ReAct loop with tools, SGR, budget, condensation

Replaces diffgraph's _solve_phase and _plan_phase.
"""
from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import AgentConfig, PusherType
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
    topology_node: Optional[str] = None
    steps: list[StepRecord] = field(default_factory=list)
    total_tokens: int = 0
    total_steps: int = 0


@dataclass
class AgentResult:
    agent_id: str = ""
    agent_name: str = ""
    output: Any = None  # parsed done() output or single-shot response
    sgr_history: list[SGREntry] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    budget_state: Optional[BudgetState] = None
    trace: AgentTrace = field(default_factory=AgentTrace)


class Agent:
    """
    Core agent: runs a single-shot or ReAct loop with tools, SGR,
    budget tracking, and message condensation.
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
        topology_node: Optional[str] = None,
        depth: int = 0,
        context_messages: Optional[list[dict]] = None,
        prompt_vars: Optional[dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.registry = tool_registry
        self.llm = llm
        self.model = model
        self.event_bus = event_bus or EventBus()
        self.agent_id = agent_id or str(uuid.uuid4())[:8]
        self.parent_id = parent_id
        self.topology_node = topology_node
        self.depth = depth
        self.context_messages = context_messages or []
        self.prompt_vars = prompt_vars or {}

        # Runtime state
        self.sgr = SGRTracker(config.sgr_extensions) if config.sgr else None
        self.budget_tracker = BudgetTracker(config.budget, self.event_bus)
        self.budget_state: Optional[BudgetState] = None
        self._done_output: Any = None
        self._done_called = False

        # Resolve LLM params
        self._base_llm_params = self._resolve_base_params()

        # Spawn/fork/plan callbacks (set by runner)
        self.on_spawn: Optional[Callable] = None  # (spawn_request) -> AgentResult
        self.on_fork: Optional[Callable] = None    # (fork_request) -> list[AgentResult]
        self.on_plan: Optional[Callable] = None    # (plan_request) -> dict

        # Auto-spawn tracking
        self._auto_spawn_counts: dict[int, int] = {}  # rule_index -> fire count

    def run(self) -> AgentResult:
        """Main entry point."""
        self.event_bus.emit(EventType.AGENT_STARTED,
                           agent_id=self.agent_id, agent_name=self.config.name,
                           parent_id=self.parent_id, node=self.topology_node)

        if self.config.budget.max_steps <= 1 and not self.config.tools:
            return self._run_single()
        return self._run_react()

    # ── Single-shot mode ──────────────────────────────────────────────────────

    def _run_single(self) -> AgentResult:
        """One LLM call, no tools. For strategist-style agents."""
        messages = self._build_messages()
        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._base_llm_params.get("temperature", 0),
                stream=False,
            )
            content = (response.choices[0].message.content or "").strip()
            # Try to parse as JSON
            output = self._try_parse_json(content)
        except Exception as exc:
            log.warning("single-shot agent '%s' failed: %s", self.config.name, exc)
            output = None

        return AgentResult(
            agent_id=self.agent_id,
            agent_name=self.config.name,
            output=output,
            messages=messages,
            trace=AgentTrace(
                agent_id=self.agent_id,
                agent_name=self.config.name,
                parent_id=self.parent_id,
                topology_node=self.topology_node,
            ),
        )

    # ── ReAct loop ────────────────────────────────────────────────────────────

    def _run_react(self) -> AgentResult:
        """ReAct loop: stream LLM -> dispatch tools -> check budget -> repeat."""
        messages = self._build_messages()
        self.budget_state = self.budget_tracker.start()
        trace = AgentTrace(
            agent_id=self.agent_id,
            agent_name=self.config.name,
            parent_id=self.parent_id,
            topology_node=self.topology_node,
        )

        tool_names = self._build_tool_names()
        tools_schema = self.registry.to_openai_schema(tool_names)
        steps_since_reflect = 0

        for step in range(self.config.budget.max_steps):
            if self.budget_state.exhausted:
                self.event_bus.emit(EventType.AGENT_FORCED_DONE,
                                   agent_id=self.agent_id, reason="token limit",
                                   tok_in=self.budget_state.tokens_in,
                                   tok_out=self.budget_state.tokens_out,
                                   tok_cached=self.budget_state.tokens_cached)
                break

            # Check pushers and apply actions
            actions = self.budget_tracker.check_pushers(self.budget_state)
            current_tools_schema = tools_schema
            for action in actions:
                current_tools_schema = self._apply_pusher(
                    action, messages, tools_schema, current_tools_schema
                )

            # SGR nudge: if interval reached and SGR enabled
            if (self.config.sgr and self.sgr and
                    steps_since_reflect >= self.config.sgr_interval and
                    not self._done_called):
                # Soft nudge — don't force, just remind
                pass  # The prompt already tells the agent to reflect every N steps

            # Auto-spawn check (on_start for step 0, on_stuck/on_low_confidence for others)
            if not self._done_called:
                auto_result = self._check_auto_spawn(step, messages, "on_start" if step == 0 else None)
                if auto_result:
                    messages.append({"role": "user", "content": auto_result})

            # Resolve LLM params for this step
            step_params = self._resolve_step_params(step)

            # Stream callback
            def _on_token(tn: str, args: str, tok: int) -> None:
                self.event_bus.emit(EventType.AGENT_STREAM,
                                   agent_id=self.agent_id, step=step,
                                   tool_name=tn, args_preview=args[:80], tok=tok)

            try:
                response = stream_llm(
                    self.llm, step_params.pop("model", self.model),
                    messages, current_tools_schema,
                    tool_choice="required",
                    on_token=_on_token,
                    **step_params,
                )
            except Exception as exc:
                log.warning("agent '%s' step %d failed: %s", self.config.name, step, exc)
                break

            # Update token budget
            if response.usage:
                self.budget_tracker.update_tokens(
                    self.budget_state,
                    total_tokens=response.usage.total_tokens,
                    tokens_in=response.usage.prompt_tokens,
                    tokens_out=response.usage.completion_tokens,
                    tokens_cached=_extract_cached(response.usage),
                )
            self.budget_state.steps_used = step + 1

            msg = response.choices[0].message
            if not msg.tool_calls:
                break

            # Separate done from dispatchable tools
            dispatch_tcs = []
            for tc in msg.tool_calls:
                if tc.function.name == "done":
                    pass  # handled below
                else:
                    dispatch_tcs.append(tc)

            # Emit events for each tool call
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name == "reflect":
                    if self.sgr:
                        entry = self.sgr.record(step, args)
                        self.event_bus.emit(EventType.AGENT_REFLECT,
                                           agent_id=self.agent_id, step=step, **args)
                        steps_since_reflect = 0
                else:
                    self.event_bus.emit(EventType.AGENT_STEP,
                                       agent_id=self.agent_id, step=step,
                                       tool=tc.function.name, args=args,
                                       tok_in=self.budget_state.tokens_in,
                                       tok_out=self.budget_state.tokens_out,
                                       tok_cached=self.budget_state.tokens_cached)
                    steps_since_reflect += 1

            # Parallel tool dispatch
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

            # Build tool results
            step_record = StepRecord(step=step, llm_params=step_params)
            findings_from_done = None

            for tc in msg.tool_calls:
                if tc.function.name == "done":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    self._done_output = args.get("findings", args)
                    self._done_called = True
                    findings_from_done = self._done_output
                    content = "Review submitted."
                elif tc.function.name == "reflect":
                    content = "Reflection noted."
                elif tc.function.name == "spawn_agent":
                    content = self._handle_spawn_tool(tc)
                elif tc.function.name == "fork":
                    content = self._handle_fork_tool(tc)
                elif tc.function.name == "plan":
                    content = self._handle_plan_tool(tc)
                else:
                    result = dispatch_results.get(tc.id, "")
                    result_count = len(result) if isinstance(result, list) else None
                    self.event_bus.emit(EventType.AGENT_TOOL_RESULT,
                                       agent_id=self.agent_id, step=step,
                                       tool=tc.function.name,
                                       result_len=len(str(result)),
                                       result_count=result_count)
                    content = self.registry.format_result(tc.function.name, result)
                    step_record.tool_calls.append({"name": tc.function.name})
                    step_record.results.append(content[:200])

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

            if response.usage:
                step_record.tokens_in = response.usage.prompt_tokens
                step_record.tokens_out = response.usage.completion_tokens
                step_record.tokens_cached = _extract_cached(response.usage)
            trace.steps.append(step_record)

            # Auto-spawn after reflect (on_reflect, on_low_confidence, on_many_questions)
            if not self._done_called and self.sgr and steps_since_reflect == 0:
                auto_result = self._check_auto_spawn(step, messages, "on_reflect")
                if auto_result:
                    messages.append({"role": "user", "content": auto_result})

            if findings_from_done is not None:
                trace.total_tokens = self.budget_state.tokens_used
                trace.total_steps = self.budget_state.steps_used
                self.event_bus.emit(EventType.AGENT_DONE,
                                   agent_id=self.agent_id,
                                   output=findings_from_done,
                                   tok_in=self.budget_state.tokens_in,
                                   tok_out=self.budget_state.tokens_out,
                                   tok_cached=self.budget_state.tokens_cached)
                return AgentResult(
                    agent_id=self.agent_id,
                    agent_name=self.config.name,
                    output=findings_from_done,
                    sgr_history=self.sgr.history if self.sgr else [],
                    messages=messages,
                    budget_state=self.budget_state,
                    trace=trace,
                )

            # Maybe condense
            messages = self._maybe_condense(messages)

        # Force done — step or token limit
        self.event_bus.emit(EventType.AGENT_FORCED_DONE,
                           agent_id=self.agent_id,
                           reason="step limit" if not self.budget_state.exhausted else "token limit",
                           tok_in=self.budget_state.tokens_in,
                           tok_out=self.budget_state.tokens_out,
                           tok_cached=self.budget_state.tokens_cached)
        forced = self._force_done(messages, tools_schema)

        trace.total_tokens = self.budget_state.tokens_used
        trace.total_steps = self.budget_state.steps_used
        return AgentResult(
            agent_id=self.agent_id,
            agent_name=self.config.name,
            output=forced,
            sgr_history=self.sgr.history if self.sgr else [],
            messages=messages,
            budget_state=self.budget_state,
            trace=trace,
        )

    # ── Message building ──────────────────────────────────────────────────────

    def _build_messages(self) -> list[dict]:
        """Assemble system prompt + context messages."""
        prompt_text = load_prompt(self.config.system_prompt)
        if self.prompt_vars:
            prompt_text = interpolate(prompt_text, **self.prompt_vars)

        messages = [{"role": "system", "content": prompt_text}]
        messages.extend(self.context_messages)
        return messages

    def _build_tool_names(self) -> list[str]:
        """Agent's configured tools + auto-added builtins."""
        names = list(self.config.tools)

        if self.config.sgr and self.registry.has("reflect"):
            if "reflect" not in names:
                names.append("reflect")

        if self.registry.has("done"):
            if "done" not in names:
                names.append("done")

        for st in self.config.spawn_tools:
            if self.registry.has(st) and st not in names:
                names.append(st)

        return names

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch_tools(self, tool_calls: list) -> dict[str, Any]:
        """Parallel tool dispatch via ThreadPoolExecutor."""
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

    # ── Spawn / Fork handlers ─────────────────────────────────────────────────

    def _handle_spawn_tool(self, tc) -> str:
        """Handle spawn_agent tool call."""
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if self.on_spawn:
            try:
                child_result = self.on_spawn(args)
                return json.dumps({
                    "status": "completed",
                    "output": child_result.output,
                }, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"spawn failed: {e}"
        return "spawn not available at this depth"

    def _handle_fork_tool(self, tc) -> str:
        """Handle fork() tool call."""
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if self.on_fork:
            try:
                results = self.on_fork(args)
                return json.dumps({
                    "status": "completed",
                    "fork_count": len(results),
                    "outputs": [r.output for r in results],
                }, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"fork failed: {e}"
        return "fork not available at this depth"

    def _handle_plan_tool(self, tc) -> str:
        """Handle plan() tool call — spawns a planner sub-agent."""
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if self.on_plan:
            try:
                result = self.on_plan(args)
                if isinstance(result, dict):
                    return json.dumps(result, indent=2, ensure_ascii=False, default=str)
                return str(result)
            except Exception as e:
                return f"plan failed: {e}"
        # Fallback: run plan inline via a single LLM call
        return self._run_inline_plan(args)

    def _run_inline_plan(self, args: dict) -> str:
        """Run a lightweight plan via a single LLM call (no sub-agent needed)."""
        from .tools.builtin import DEFAULT_PLAN_PROMPT
        goal = args.get("goal", "")
        constraints = args.get("constraints", "")
        output_hint = args.get("output_hint", "")

        user_content = f"Goal: {goal}"
        if constraints:
            user_content += f"\nConstraints: {constraints}"
        if output_hint:
            user_content += f"\nOutput format: {output_hint}"

        # Add SGR context if available
        if self.sgr and self.sgr.last:
            user_content += f"\n\nCurrent knowledge:\n{self.sgr.last.learned}"
            if self.sgr.last.questions_remaining:
                user_content += "\nOpen questions:\n" + "\n".join(
                    f"- {q}" for q in self.sgr.last.questions_remaining
                )

        try:
            resp = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": DEFAULT_PLAN_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
                stream=False,
            )
            content = (resp.choices[0].message.content or "").strip()
            # Try to parse as JSON for cleaner output
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return content
        except Exception as e:
            return f"plan generation failed: {e}"

    # ── Auto-spawn ────────────────────────────────────────────────────────────

    def _check_auto_spawn(self, step: int, messages: list[dict],
                          event: str | None = None) -> str | None:
        """
        Check auto-spawn rules and fire if conditions met.
        Returns a string to inject into messages, or None.
        """
        if not self.config.auto_spawn:
            return None

        for idx, rule in enumerate(self.config.auto_spawn):
            if not rule.enabled:
                continue
            count = self._auto_spawn_counts.get(idx, 0)
            if count >= rule.max_spawns:
                continue
            if not self._should_auto_spawn(rule, step, event):
                continue

            # Fire!
            self._auto_spawn_counts[idx] = count + 1

            self.event_bus.emit(EventType.AGENT_SPAWNED,
                                parent_id=self.agent_id,
                                child_id=f"{self.agent_id}_auto_{idx}_{count}",
                                agent_name=f"auto_{rule.spawn_type}",
                                trigger=rule.trigger)

            if rule.spawn_type == "plan":
                result = self._auto_spawn_plan(rule)
            elif rule.spawn_type == "sgr":
                result = self._auto_spawn_sgr(rule)
            else:
                continue

            if result and rule.inject_result:
                return f"[Auto-spawned {rule.spawn_type} agent result]\n{result}"

        return None

    def _should_auto_spawn(self, rule, step: int, event: str | None) -> bool:
        """Check if an auto-spawn rule should fire."""
        trigger = rule.trigger

        if trigger == "on_start" and event == "on_start" and step == 0:
            return True

        if trigger == "on_reflect" and event == "on_reflect":
            return True

        if trigger == "on_low_confidence" and self.sgr and self.sgr.last:
            if self.sgr.last.confidence == "low" and step >= rule.threshold:
                return True

        if trigger == "on_many_questions" and self.sgr and self.sgr.last:
            if len(self.sgr.last.questions_remaining) >= rule.threshold:
                return True

        if trigger == "on_stuck" and event == "on_stuck":
            return True

        return False

    def _auto_spawn_plan(self, rule) -> str | None:
        """Auto-spawn a plan sub-agent."""
        from .tools.builtin import DEFAULT_PLAN_PROMPT

        # Build goal from SGR state
        goal = "Create a plan for the current investigation."
        if self.sgr and self.sgr.last:
            goal = f"Plan next steps. Current knowledge: {self.sgr.last.learned[:300]}"
            if self.sgr.last.questions_remaining:
                goal += "\nOpen questions: " + "; ".join(self.sgr.last.questions_remaining[:5])

        prompt = rule.plan_prompt or DEFAULT_PLAN_PROMPT

        if self.on_plan:
            try:
                result = self.on_plan({"goal": goal, "_auto": True, "_prompt": prompt})
                return json.dumps(result, indent=2, default=str) if isinstance(result, dict) else str(result)
            except Exception as e:
                log.warning("auto-spawn plan failed: %s", e)

        # Inline fallback
        return self._run_inline_plan({"goal": goal})

    def _auto_spawn_sgr(self, rule) -> str | None:
        """Auto-spawn an SGR sub-agent for a specific question."""
        if not self.sgr or not self.sgr.last or not self.sgr.last.questions_remaining:
            return None

        # Pick the first open question
        question = self.sgr.last.questions_remaining[0]

        if self.on_spawn:
            try:
                result = self.on_spawn({
                    "agent": rule.child_agent or self.config.name,
                    "focus": question,
                    "context_handoff": rule.context_handoff,
                    "wait": True,
                    "_auto": True,
                })
                if hasattr(result, 'output'):
                    return json.dumps({
                        "question": question,
                        "result": result.output,
                    }, indent=2, default=str)
            except Exception as e:
                log.warning("auto-spawn sgr failed: %s", e)

        return None

    # ── Budget pushers ────────────────────────────────────────────────────────

    def _apply_pusher(self, action: PusherAction, messages: list[dict],
                      all_tools: list[dict], current_tools: list[dict]) -> list[dict]:
        """Apply a pusher action and return (possibly modified) tools schema."""
        if action.type == PusherType.NUDGE:
            messages.append({"role": "user", "content": action.message})
            return current_tools
        elif action.type == PusherType.FORCE_REFLECT:
            # Restrict to only reflect tool
            return [t for t in all_tools if t["function"]["name"] == "reflect"]
        elif action.type == PusherType.FORCE_DONE:
            # Restrict to only done tool
            return [t for t in all_tools if t["function"]["name"] == "done"]
        elif action.type == PusherType.CUSTOM and action.handler:
            try:
                action.handler(messages, self.budget_state)
            except Exception as e:
                log.warning("custom pusher failed: %s", e)
            return current_tools
        return current_tools

    # ── LLM params ────────────────────────────────────────────────────────────

    def _resolve_base_params(self) -> dict:
        """Static LLM params from config."""
        if not self.config.llm_params:
            return {"temperature": 0}
        lp = self.config.llm_params
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
        return params

    def _resolve_step_params(self, step: int) -> dict:
        """Resolve LLM params for a specific step (static + schedule overrides)."""
        # Start with base params
        params = dict(self._base_llm_params)
        # Adaptive schedules are applied by the runner via set_adaptive_resolver()
        # For now return base. Phase 7 adds the adaptive resolver integration.
        return params

    # ── Condensation ──────────────────────────────────────────────────────────

    def _maybe_condense(self, messages: list[dict]) -> list[dict]:
        """Check token threshold, apply condensation if needed."""
        cfg = self.config.condensation
        if not cfg or not cfg.enabled:
            return messages
        if not should_condense(messages, cfg.trigger):
            return messages

        condenser = get_condenser(cfg.strategy)
        sgr_entries = self.sgr.history if self.sgr else []

        self.event_bus.emit(EventType.CONDENSATION_TRIGGERED,
                           agent_id=self.agent_id,
                           strategy=cfg.strategy.value,
                           message_count=len(messages))

        return condenser.condense(messages, cfg, sgr_entries, self.llm, self.model)

    # ── Force done ────────────────────────────────────────────────────────────

    def _force_done(self, messages: list[dict], tools_schema: list[dict]) -> Any:
        """Force a done() call when budget is exhausted."""
        messages.append({
            "role": "user",
            "content": "Step limit reached. Call done() now with all findings you have so far.",
        })
        done_tools = [t for t in tools_schema if t["function"]["name"] == "done"]
        if not done_tools:
            return None
        try:
            response = stream_llm(
                self.llm, self.model, messages, done_tools,
                tool_choice="required",
            )
            msg = response.choices[0].message
            if msg.tool_calls:
                args = json.loads(msg.tool_calls[0].function.arguments or "{}")
                return args.get("findings", args)
        except Exception as exc:
            log.warning("agent '%s' force done failed: %s", self.config.name, exc)
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _try_parse_json(text: str) -> Any:
        """Try to parse text as JSON, stripping markdown fences if present."""
        content = text.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            content = content.rsplit("```", 1)[0].strip()
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
