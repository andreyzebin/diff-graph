"""
Topology: DAG of agent nodes connected by edges with handoff modes.

Supports programmatic and YAML-based topology definition, DAG validation,
and node/edge manipulation.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from .types import EdgeConfig, NodeConfig, NodeType, TopologyConfig

log = logging.getLogger(__name__)


@dataclass
class Edge:
    from_node: str
    to_node: str
    handoff_mode: str = "findings_only"


@dataclass
class Node:
    config: NodeConfig
    inbound_edges: list[Edge] = field(default_factory=list)
    outbound_edges: list[Edge] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def agent(self) -> str:
        return self.config.agent

    @property
    def type(self) -> NodeType:
        return self.config.type


class Topology:
    """A directed acyclic graph of agent nodes."""

    def __init__(self, config: Optional[TopologyConfig] = None) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self.name = ""
        self.shared_tools_config: dict = {}
        self.feedback_loops_config: list = []

        if config:
            self.name = config.name
            self.shared_tools_config = config.shared_tools
            self.feedback_loops_config = config.feedback_loops
            for nc in config.nodes:
                self.add_node(nc)
            for ec in config.edges:
                self.add_edge(ec)

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges)

    def add_node(self, config: NodeConfig) -> Node:
        """Add a node from config."""
        node = Node(config=config)
        self._nodes[config.id] = node
        return node

    def add_edge(self, config: EdgeConfig) -> Edge:
        """Add an edge between existing nodes."""
        edge = Edge(
            from_node=config.from_node,
            to_node=config.to_node,
            handoff_mode=config.context_handoff,
        )
        self._edges.append(edge)

        if config.from_node in self._nodes:
            self._nodes[config.from_node].outbound_edges.append(edge)
        if config.to_node in self._nodes:
            self._nodes[config.to_node].inbound_edges.append(edge)

        return edge

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_roots(self) -> list[Node]:
        """Nodes with no inbound edges (start nodes)."""
        has_inbound = set()
        for edge in self._edges:
            has_inbound.add(edge.to_node)
        return [n for nid, n in self._nodes.items() if nid not in has_inbound]

    def get_terminals(self) -> list[Node]:
        """Nodes with no outbound edges (end nodes)."""
        has_outbound = set()
        for edge in self._edges:
            has_outbound.add(edge.from_node)
        return [n for nid, n in self._nodes.items() if nid not in has_outbound]

    def get_children(self, node_id: str) -> list[Node]:
        """Nodes directly downstream of node_id."""
        children = []
        for edge in self._edges:
            if edge.from_node == node_id and edge.to_node in self._nodes:
                children.append(self._nodes[edge.to_node])
        return children

    def get_parents(self, node_id: str) -> list[Node]:
        """Nodes directly upstream of node_id."""
        parents = []
        for edge in self._edges:
            if edge.to_node == node_id and edge.from_node in self._nodes:
                parents.append(self._nodes[edge.from_node])
        return parents

    def topological_order(self) -> list[Node]:
        """Return nodes in topological order. Raises ValueError if cycle detected."""
        in_degree: dict[str, int] = defaultdict(int)
        for nid in self._nodes:
            in_degree.setdefault(nid, 0)
        for edge in self._edges:
            in_degree[edge.to_node] += 1

        queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
        order: list[Node] = []

        while queue:
            nid = queue.popleft()
            node = self._nodes.get(nid)
            if node:
                order.append(node)
            for edge in self._edges:
                if edge.from_node == nid:
                    in_degree[edge.to_node] -= 1
                    if in_degree[edge.to_node] == 0:
                        queue.append(edge.to_node)

        if len(order) != len(self._nodes):
            raise ValueError(
                f"topology '{self.name}' has a cycle — "
                f"processed {len(order)}/{len(self._nodes)} nodes"
            )
        return order

    def validate(self, agent_names: set[str], tool_names: Optional[set[str]] = None) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors: list[str] = []

        # Check agents exist
        for nid, node in self._nodes.items():
            if node.agent != "$dynamic" and node.agent and node.agent not in agent_names:
                errors.append(f"node '{nid}': unknown agent '{node.agent}'")

        # Check edge references
        node_ids = set(self._nodes.keys())
        for edge in self._edges:
            if edge.from_node not in node_ids:
                errors.append(f"edge: unknown from_node '{edge.from_node}'")
            if edge.to_node not in node_ids:
                errors.append(f"edge: unknown to_node '{edge.to_node}'")

        # Check join nodes have sources
        for nid, node in self._nodes.items():
            if node.type == NodeType.JOIN:
                if not node.config.sources and not node.inbound_edges:
                    errors.append(f"join node '{nid}': no sources defined")

        # Check DAG
        try:
            self.topological_order()
        except ValueError as e:
            errors.append(str(e))

        return errors
