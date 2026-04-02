from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Dependency:
    """An annotated directed edge from a module to one of its dependencies."""
    name: str           # simple class/module name: "Order"
    fqn: str            # fully qualified name from imports: "com.flowmart.orders.model.Order"
    usage: str          # how this dep is used (extracted by LLM from source)
    file_path: str = "" # resolved by agent; empty until resolved or if external


@dataclass
class Symbol:
    name: str
    kind: str  # METHOD | CLASS | FIELD | INTERFACE | ENUM | FUNCTION | MODULE
    signature: str
    summary: str
    annotations: list[str]
    start_line: int
    end_line: int
    is_changed: bool = False
    is_expanded: bool = False  # marked by review agent as important — show full body
    full_code: Optional[str] = None


@dataclass
class Module:
    id: str           # relative path: "src/.../PaymentService.java"
    name: str         # "PaymentService"
    lang: str         # "java" | "python" | "typescript" | "go" | "unknown"
    summary: str
    symbols: list[Symbol]
    dependencies: list[Dependency]  # annotated edges to direct deps


@dataclass
class MetaModel:
    modules: dict[str, Module] = field(default_factory=dict)
    changed_module_ids: list[str] = field(default_factory=list)

    def add(self, module: Module) -> None:
        self.modules[module.id] = module

    def compute_depths(self) -> dict[str, int]:
        """
        BFS from changed modules through dependency edges.
        Returns {module_id: depth} where depth=0 is changed, depth>0 is deps,
        depth=-1 is callers (modules with a dep pointing to a changed module).
        """
        changed_ids = set(self.changed_module_ids)
        depths: dict[str, int] = {mid: 0 for mid in changed_ids if mid in self.modules}

        queue: deque[str] = deque(depths.keys())
        while queue:
            mid = queue.popleft()
            mod = self.modules.get(mid)
            if not mod:
                continue
            for dep in mod.dependencies:
                if dep.file_path and dep.file_path in self.modules and dep.file_path not in depths:
                    depths[dep.file_path] = depths[mid] + 1
                    queue.append(dep.file_path)

        # Callers: in graph but not reachable via forward edges, yet depend on a changed module
        for mid, mod in self.modules.items():
            if mid not in depths:
                for dep in mod.dependencies:
                    if dep.file_path in changed_ids:
                        depths[mid] = -1
                        break

        return depths

    def to_json(self) -> list[dict]:
        """Serialize the graph to a JSON-serializable list of module dicts."""
        result = []
        for module in self.modules.values():
            symbols = []
            for s in module.symbols:
                sym: dict = {
                    "name": s.name,
                    "kind": s.kind,
                    "signature": s.signature,
                    "summary": s.summary,
                    "annotations": s.annotations,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "is_changed": s.is_changed,
                }
                symbols.append(sym)
            deps = [
                {
                    "name": d.name,
                    "fqn": d.fqn,
                    "usage": d.usage,
                    "file_path": d.file_path,
                }
                for d in module.dependencies
            ]
            result.append({
                "id": module.id,
                "name": module.name,
                "lang": module.lang,
                "summary": module.summary,
                "dependencies": deps,
                "symbols": symbols,
            })
        return result
