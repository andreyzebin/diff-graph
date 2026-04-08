"""
TopologyRunner: executes a Topology DAG.

Walks nodes in topological order, instantiates Agents, manages
fan_out/join concurrency, spawn/fork, and handoff between nodes.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from .types import (
    AgentConfig,
    BudgetConfig,
    NodeType,
    OrchestraConfig,
    TopologyConfig,
)
from .agent import Agent, AgentResult
from .budget import BudgetTracker
from .events import EventBus, EventType
from .handoff import get_handoff
from .topology import Edge, Node, Topology
from .tools.registry import ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class TopologyResult:
    """Result of a full topology execution."""
    outputs: dict[str, Any] = field(default_factory=dict)  # node_id -> AgentResult
    final_output: Any = None
    execution_tree: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    total_wall_time: float = 0.0


class TopologyRunner:
    """Executes a Topology by walking the DAG."""

    def __init__(
        self,
        topology: Topology,
        config: OrchestraConfig,
        tool_registry: ToolRegistry,
        llm: Any,
        model: str,
        event_bus: Optional[EventBus] = None,
        global_budget: Optional[BudgetConfig] = None,
    ) -> None:
        self.topology = topology
        self.config = config
        self.registry = tool_registry
        self.llm = llm
        self.model = model
        self.event_bus = event_bus or EventBus()
        self.global_budget = global_budget
        self._results: dict[str, AgentResult | list[AgentResult]] = {}

    def run(self) -> TopologyResult:
        """Execute the full topology. Blocks until complete."""
        start_time = time.time()
        self.event_bus.emit(EventType.TOPOLOGY_STARTED, topology=self.topology.name)

        try:
            order = self.topology.topological_order()
        except ValueError as e:
            log.error("topology validation failed: %s", e)
            return TopologyResult()

        for node in order:
            self.event_bus.emit(EventType.NODE_STARTED,
                               node_id=node.id, agent=node.agent, type=node.type.value)
            result = self._execute_node(node)
            self._results[node.id] = result
            self.event_bus.emit(EventType.NODE_DONE, node_id=node.id)

        # Build result
        terminals = self.topology.get_terminals()
        final_output = None
        if terminals:
            last = self._results.get(terminals[-1].id)
            if isinstance(last, AgentResult):
                final_output = last.output
            elif isinstance(last, list) and last:
                final_output = [r.output for r in last]

        total_tokens = sum(
            r.budget_state.tokens_used if isinstance(r, AgentResult) and r.budget_state else 0
            for r in self._results.values()
            if isinstance(r, AgentResult)
        )
        # Sum fan_out lists too
        for r in self._results.values():
            if isinstance(r, list):
                total_tokens += sum(
                    ar.budget_state.tokens_used if ar.budget_state else 0
                    for ar in r
                )

        result = TopologyResult(
            outputs={k: v for k, v in self._results.items()},
            final_output=final_output,
            total_tokens=total_tokens,
            total_wall_time=time.time() - start_time,
        )
        self.event_bus.emit(EventType.TOPOLOGY_DONE,
                           topology=self.topology.name,
                           total_tokens=total_tokens)
        return result

    # ── Node execution ────────────────────────────────────────────────────────

    def _execute_node(self, node: Node) -> AgentResult | list[AgentResult]:
        """Run one node. Dispatches by type."""
        if node.type == NodeType.SINGLE:
            return self._execute_agent(node)
        elif node.type == NodeType.REACT:
            return self._execute_agent(node)
        elif node.type == NodeType.FAN_OUT:
            return self._execute_fan_out(node)
        elif node.type == NodeType.JOIN:
            return self._execute_join(node)
        else:
            log.warning("unknown node type: %s", node.type)
            return AgentResult(agent_name=node.agent)

    def _execute_agent(self, node: Node) -> AgentResult:
        """Run a single or react agent for a node."""
        agent_config = self._resolve_agent_config(node)
        if agent_config is None:
            log.error("no agent config for node '%s' (agent='%s')", node.id, node.agent)
            return AgentResult(agent_name=node.agent)

        context_messages = self._build_context(node)
        prompt_vars = self._build_prompt_vars(node)

        agent = Agent(
            config=agent_config,
            tool_registry=self.registry,
            llm=self.llm,
            model=agent_config.llm_params.model if agent_config.llm_params and agent_config.llm_params.model else self.model,
            event_bus=self.event_bus,
            topology_node=node.id,
            context_messages=context_messages,
            prompt_vars=prompt_vars,
        )

        # Wire up spawn/fork/plan callbacks
        agent.on_spawn = lambda req: self._handle_spawn(agent, req)
        agent.on_fork = lambda req: self._handle_fork(agent, req)
        agent.on_plan = lambda req: self._handle_plan(agent, req)

        return agent.run()

    def _execute_fan_out(self, node: Node) -> list[AgentResult]:
        """Spawn N agents in parallel."""
        parent_result = self._get_parent_result(node)
        tasks = self._resolve_fan_out_tasks(node, parent_result)

        if not tasks:
            return []

        results: list[AgentResult] = []
        agent_config = self._resolve_agent_config(node)
        if agent_config is None:
            return []

        def _run_one(task: dict) -> AgentResult:
            # Clone config with task-specific focus
            task_config = AgentConfig(
                name=f"{agent_config.name}_{task.get('id', 'task')}",
                system_prompt=agent_config.system_prompt,
                sgr=agent_config.sgr,
                sgr_interval=agent_config.sgr_interval,
                sgr_extensions=agent_config.sgr_extensions,
                tools=list(agent_config.tools),
                spawn_tools=list(agent_config.spawn_tools),
                output_schema=agent_config.output_schema,
                budget=agent_config.budget,
                condensation=agent_config.condensation,
                fork=agent_config.fork,
                auto_fork=agent_config.auto_fork,
                llm_params=agent_config.llm_params,
            )

            context = self._build_context(node)
            # Add task focus as context
            context.append({
                "role": "user",
                "content": f"Your specific task:\n{json.dumps(task, indent=2)}",
            })

            agent = Agent(
                config=task_config,
                tool_registry=self.registry,
                llm=self.llm,
                model=task_config.llm_params.model if task_config.llm_params and task_config.llm_params.model else self.model,
                event_bus=self.event_bus,
                topology_node=node.id,
                context_messages=context,
            )
            agent.on_spawn = lambda req: self._handle_spawn(agent, req)
            return agent.run()

        if node.config.parallel and len(tasks) > 1:
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = {executor.submit(_run_one, t): t for t in tasks}
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        log.warning("fan_out task failed: %s", e)
        else:
            for task in tasks:
                results.append(_run_one(task))

        return results

    def _execute_join(self, node: Node) -> AgentResult:
        """Wait for all sources, apply handoff per edge, run join agent."""
        source_results: list[AgentResult] = []
        for src_id in (node.config.sources or []):
            r = self._results.get(src_id)
            if isinstance(r, list):
                source_results.extend(r)
            elif isinstance(r, AgentResult):
                source_results.append(r)

        # Also gather from inbound edges
        for edge in node.inbound_edges:
            r = self._results.get(edge.from_node)
            if r and edge.from_node not in (node.config.sources or []):
                if isinstance(r, list):
                    source_results.extend(r)
                elif isinstance(r, AgentResult):
                    source_results.append(r)

        # Build context messages: one section per source agent
        context_messages: list[dict] = []
        for i, sr in enumerate(source_results):
            # Find the edge for this source to get handoff mode
            handoff_mode = "findings_only"
            for edge in node.inbound_edges:
                if edge.from_node == sr.agent_name or True:  # use edge's mode
                    handoff_mode = edge.handoff_mode
                    break

            handoff = get_handoff(handoff_mode)
            agent_msgs = handoff.apply(sr.messages, sr.sgr_history, sr.output, self.llm, self.model)
            if agent_msgs:
                header = {"role": "user", "content": f"--- Agent {i+1}: {sr.agent_name} ---"}
                context_messages.append(header)
                context_messages.extend(agent_msgs)

        agent_config = self._resolve_agent_config(node)
        if agent_config is None:
            return AgentResult()

        agent = Agent(
            config=agent_config,
            tool_registry=self.registry,
            llm=self.llm,
            model=agent_config.llm_params.model if agent_config.llm_params and agent_config.llm_params.model else self.model,
            event_bus=self.event_bus,
            topology_node=node.id,
            context_messages=context_messages,
        )
        return agent.run()

    # ── Context building ──────────────────────────────────────────────────────

    def _build_context(self, node: Node) -> list[dict]:
        """Build context messages from parent results via handoff modes."""
        context: list[dict] = []
        for edge in node.inbound_edges:
            parent_result = self._results.get(edge.from_node)
            if parent_result is None:
                continue

            # If parent was fan_out (list of results), we already handled it
            if isinstance(parent_result, list):
                continue

            handoff = get_handoff(edge.handoff_mode)
            msgs = handoff.apply(
                parent_result.messages,
                parent_result.sgr_history,
                parent_result.output,
                self.llm,
                self.model,
            )
            context.extend(msgs)
        return context

    def _build_prompt_vars(self, node: Node) -> dict[str, str]:
        """Build prompt template variables from parent results."""
        vars_dict: dict[str, str] = {}
        for edge in node.inbound_edges:
            parent_result = self._results.get(edge.from_node)
            if parent_result is None:
                continue
            if isinstance(parent_result, AgentResult) and parent_result.output is not None:
                # Make parent output available as a template variable
                if isinstance(parent_result.output, (dict, list)):
                    vars_dict[edge.from_node] = json.dumps(
                        parent_result.output, indent=2, ensure_ascii=False
                    )
                else:
                    vars_dict[edge.from_node] = str(parent_result.output)
        return vars_dict

    # ── Fan-out resolution ────────────────────────────────────────────────────

    def _resolve_fan_out_tasks(self, node: Node, parent_result: Optional[AgentResult]) -> list[dict]:
        """Determine the N tasks for fan_out."""
        if node.config.spawn_from and parent_result and parent_result.output:
            # Navigate dotted path into parent output
            output = parent_result.output
            for key in node.config.spawn_from.split("."):
                if isinstance(output, dict):
                    output = output.get(key, [])
                else:
                    output = []
                    break
            if isinstance(output, list):
                return output
        return []

    def _get_parent_result(self, node: Node) -> Optional[AgentResult]:
        """Get the single parent result for a node."""
        source = node.config.source
        if source and source in self._results:
            r = self._results[source]
            if isinstance(r, AgentResult):
                return r
        # Try inbound edges
        for edge in node.inbound_edges:
            r = self._results.get(edge.from_node)
            if isinstance(r, AgentResult):
                return r
        return None

    # ── Agent config resolution ───────────────────────────────────────────────

    def _resolve_agent_config(self, node: Node) -> Optional[AgentConfig]:
        """Resolve agent config for a node."""
        if node.agent == "$dynamic":
            # Dynamic: the parent result should specify the agent
            parent = self._get_parent_result(node)
            if parent and isinstance(parent.output, dict):
                agent_name = parent.output.get("agent", "")
                if agent_name in self.config.agents:
                    return self.config.agents[agent_name]
            log.warning("dynamic node '%s': could not resolve agent", node.id)
            return None

        return self.config.agents.get(node.agent)

    # ── Plan / Spawn / Fork handling ─────────────────────────────────────────

    def _handle_plan(self, parent: Agent, request: dict) -> Any:
        """Handle a plan request — spawn a planner sub-agent."""
        from .tools.builtin import DEFAULT_PLAN_PROMPT

        goal = request.get("goal", "")
        constraints = request.get("constraints", "")
        custom_prompt = request.get("_prompt", "")

        plan_prompt = custom_prompt or DEFAULT_PLAN_PROMPT

        user_content = f"Goal: {goal}"
        if constraints:
            user_content += f"\nConstraints: {constraints}"

        # Add parent SGR context
        if parent.sgr and parent.sgr.last:
            user_content += f"\nCurrent knowledge: {parent.sgr.last.learned[:500]}"
            if parent.sgr.last.questions_remaining:
                user_content += "\nOpen questions: " + "; ".join(
                    parent.sgr.last.questions_remaining[:5]
                )

        # Allocate child budget
        child_budget = parent.budget_tracker.allocate_child(parent.budget_state, 0.1)

        plan_config = AgentConfig(
            name="planner",
            system_prompt=plan_prompt,
            sgr=False,
            budget=child_budget,
        )

        child = Agent(
            config=plan_config,
            tool_registry=self.registry,
            llm=self.llm,
            model=self.model,
            event_bus=self.event_bus,
            parent_id=parent.agent_id,
            depth=parent.depth + 1,
            context_messages=[{"role": "user", "content": user_content}],
        )

        self.event_bus.emit(EventType.AGENT_SPAWNED,
                           parent_id=parent.agent_id,
                           child_id=child.agent_id,
                           agent_name="planner")

        result = child.run()

        if child.budget_state:
            parent.budget_tracker.debit_child(parent.budget_state, child.budget_state)

        return result.output

    def _handle_spawn(self, parent: Agent, request: dict) -> AgentResult:
        """Handle a spawn_agent request from an agent."""
        agent_name = request.get("agent", "")
        agent_config = self.config.agents.get(agent_name)
        if not agent_config:
            raise ValueError(f"unknown agent: {agent_name}")

        # Build context from handoff
        handoff_mode = request.get("context_handoff", "sgr_outcomes")
        handoff = get_handoff(handoff_mode)
        context = handoff.apply(
            parent.context_messages if hasattr(parent, 'context_messages') else [],
            parent.sgr.history if parent.sgr else [],
            None,
            self.llm,
            self.model,
        )
        # Add focus
        focus = request.get("focus", "")
        if focus:
            context.append({"role": "user", "content": f"Focus: {focus}"})

        # Allocate budget from parent
        child_budget = parent.budget_tracker.allocate_child(parent.budget_state, 0.5)
        child_config = AgentConfig(
            name=agent_config.name,
            system_prompt=agent_config.system_prompt,
            sgr=agent_config.sgr,
            sgr_interval=agent_config.sgr_interval,
            tools=list(agent_config.tools),
            output_schema=agent_config.output_schema,
            budget=child_budget,
            llm_params=agent_config.llm_params,
        )

        child = Agent(
            config=child_config,
            tool_registry=self.registry,
            llm=self.llm,
            model=self.model,
            event_bus=self.event_bus,
            parent_id=parent.agent_id,
            depth=parent.depth + 1,
            context_messages=context,
        )

        self.event_bus.emit(EventType.AGENT_SPAWNED,
                           parent_id=parent.agent_id,
                           child_id=child.agent_id,
                           agent_name=agent_name)

        result = child.run()

        # Debit parent
        if child.budget_state:
            parent.budget_tracker.debit_child(parent.budget_state, child.budget_state)

        return result

    def _handle_fork(self, parent: Agent, request: dict) -> list[AgentResult]:
        """Handle a fork request from an agent."""
        branches = request.get("branches", [])
        if not branches:
            return []

        results: list[AgentResult] = []
        fork_config = parent.config.fork
        n = min(len(branches), fork_config.max_forks if fork_config else 2)

        self.event_bus.emit(EventType.FORK_STARTED,
                           parent_id=parent.agent_id, branch_count=n)

        def _run_branch(branch: dict) -> AgentResult:
            handoff_mode = fork_config.context_handoff if fork_config else "full_history"
            handoff = get_handoff(handoff_mode)
            context = handoff.apply(
                parent.context_messages if hasattr(parent, 'context_messages') else [],
                parent.sgr.history if parent.sgr else [],
                None,
                self.llm,
                self.model,
            )
            focus = branch.get("focus", "")
            if focus:
                context.append({"role": "user", "content": f"Focus: {focus}"})

            child_budget = parent.budget_tracker.allocate_child(
                parent.budget_state, 1.0 / n
            )
            child_config = AgentConfig(
                name=f"{parent.config.name}_fork",
                system_prompt=parent.config.system_prompt,
                sgr=parent.config.sgr,
                tools=list(parent.config.tools),
                output_schema=parent.config.output_schema,
                budget=child_budget,
                llm_params=parent.config.llm_params,
            )
            child = Agent(
                config=child_config,
                tool_registry=self.registry,
                llm=self.llm,
                model=self.model,
                event_bus=self.event_bus,
                parent_id=parent.agent_id,
                depth=parent.depth + 1,
                context_messages=context,
            )
            return child.run()

        with ThreadPoolExecutor(max_workers=n) as executor:
            futures = {executor.submit(_run_branch, b): b for b in branches[:n]}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    log.warning("fork branch failed: %s", e)

        self.event_bus.emit(EventType.FORK_MERGED,
                           parent_id=parent.agent_id, result_count=len(results))

        return results
