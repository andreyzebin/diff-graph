"""
Shared / collaborative tools (swarm pattern).

Thread-safe data structures that multiple agents can read/write
via normal tool calls during concurrent execution.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from .registry import ToolDef, ToolRegistry

log = logging.getLogger(__name__)


class AppendLog:
    """Thread-safe append-only log. Agents can append entries and read all."""

    def __init__(self, name: str, schema: Optional[dict] = None) -> None:
        self.name = name
        self._schema = schema
        self._entries: list[dict] = []
        self._lock = threading.Lock()

    def append(self, agent_id: str, entry: dict) -> dict:
        with self._lock:
            record = {"agent_id": agent_id, "index": len(self._entries), **entry}
            self._entries.append(record)
        return {"status": "appended", "index": record["index"]}

    def read_all(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def to_tool_defs(self) -> list[ToolDef]:
        name = self.name

        def _append(**kwargs: Any) -> str:
            agent_id = kwargs.pop("agent_id", "unknown")
            result = self.append(agent_id, kwargs)
            return json.dumps(result)

        def _read(**kwargs: Any) -> str:
            return json.dumps(self.read_all(), indent=2, ensure_ascii=False)

        return [
            ToolDef(
                name=f"{name}_append",
                description=f"Append an entry to the shared {name} log.",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "Your agent ID."},
                        "entry": {"type": "object", "description": "Data to append."},
                    },
                    "required": ["entry"],
                },
                handler=_append,
            ),
            ToolDef(
                name=f"{name}_read",
                description=f"Read all entries from the shared {name} log.",
                parameters={"type": "object", "properties": {}},
                handler=_read,
            ),
        ]


class MutexMap:
    """Thread-safe claim map. Agents claim keys; claim fails if already taken."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._claims: dict[str, str] = {}  # key -> agent_id
        self._lock = threading.Lock()

    def claim(self, agent_id: str, key: str) -> dict:
        with self._lock:
            if key in self._claims:
                owner = self._claims[key]
                if owner != agent_id:
                    return {"status": "already_claimed", "owner": owner}
            self._claims[key] = agent_id
            return {"status": "claimed"}

    def release(self, agent_id: str, key: str) -> dict:
        with self._lock:
            if key in self._claims and self._claims[key] == agent_id:
                del self._claims[key]
                return {"status": "released"}
            return {"status": "not_owned"}

    def list_claims(self) -> dict:
        with self._lock:
            return dict(self._claims)

    def to_tool_defs(self) -> list[ToolDef]:
        name = self.name

        def _claim(**kwargs: Any) -> str:
            return json.dumps(self.claim(kwargs.get("agent_id", ""), kwargs.get("key", "")))

        def _release(**kwargs: Any) -> str:
            return json.dumps(self.release(kwargs.get("agent_id", ""), kwargs.get("key", "")))

        def _list_claims(**kwargs: Any) -> str:
            return json.dumps(self.list_claims())

        return [
            ToolDef(
                name=f"{name}_claim",
                description=f"Claim a key in {name}. Fails if already claimed by another agent.",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "key": {"type": "string", "description": "Key to claim (e.g. file path)."},
                    },
                    "required": ["agent_id", "key"],
                },
                handler=_claim,
            ),
            ToolDef(
                name=f"{name}_release",
                description=f"Release a claimed key in {name}.",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "key": {"type": "string"},
                    },
                    "required": ["agent_id", "key"],
                },
                handler=_release,
            ),
            ToolDef(
                name=f"{name}_list",
                description=f"List all claims in {name}.",
                parameters={"type": "object", "properties": {}},
                handler=_list_claims,
            ),
        ]


class Blackboard:
    """Thread-safe key-value store. Last-write-wins."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def write(self, key: str, value: Any) -> dict:
        with self._lock:
            self._data[key] = value
        return {"status": "written"}

    def read(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key)

    def read_all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def to_tool_defs(self) -> list[ToolDef]:
        name = self.name

        def _write(**kwargs: Any) -> str:
            result = self.write(kwargs.get("key", ""), kwargs.get("value"))
            return json.dumps(result)

        def _read(**kwargs: Any) -> str:
            key = kwargs.get("key", "")
            if key:
                return json.dumps(self.read(key), default=str)
            return json.dumps(self.read_all(), indent=2, default=str)

        return [
            ToolDef(
                name=f"{name}_write",
                description=f"Write a key-value pair to {name} blackboard.",
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"description": "Value to store (any type)."},
                    },
                    "required": ["key", "value"],
                },
                handler=_write,
            ),
            ToolDef(
                name=f"{name}_read",
                description=f"Read from {name} blackboard. Omit key to read all.",
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Key to read. Omit for all."},
                    },
                },
                handler=_read,
            ),
        ]


class SharedToolFactory:
    """Factory for creating shared tools from config."""

    @staticmethod
    def create(name: str, config: dict) -> AppendLog | MutexMap | Blackboard:
        tool_type = config.get("type", "blackboard")
        if tool_type == "append_log":
            return AppendLog(name, schema=config.get("schema"))
        elif tool_type == "mutex_map":
            return MutexMap(name)
        elif tool_type == "blackboard":
            return Blackboard(name)
        else:
            raise ValueError(f"unknown shared tool type: {tool_type}")

    @staticmethod
    def inject_tools(
        shared_tools_config: dict[str, dict],
        registry: ToolRegistry,
    ) -> dict[str, Any]:
        """Create shared tools from config and register them in the registry."""
        instances: dict[str, Any] = {}
        for name, config in shared_tools_config.items():
            instance = SharedToolFactory.create(name, config)
            instances[name] = instance
            for tool_def in instance.to_tool_defs():
                registry.register_tool_def(tool_def)
        return instances
