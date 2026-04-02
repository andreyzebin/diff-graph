from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Dependency:
    """An annotated directed edge from a module to one of its dependencies."""
    name: str           # simple class/module name: "Order"
    fqn: str            # fully qualified name from imports: "com.flowmart.orders.model.Order"
    usage: str          # how this dep is used (extracted by LLM from source)
    file_path: str = "" # resolved by agent; empty until resolved or if external
    usage_summary: str = ""  # richer summary produced by resolver agent


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
    full_code: Optional[str] = None
    before_code: Optional[str] = None


@dataclass
class Module:
    id: str           # relative path: "src/.../PaymentService.java"
    name: str         # "PaymentService"
    lang: str         # "java" | "python" | "typescript" | "go" | "unknown"
    summary: str
    symbols: list[Symbol]
    dependencies: list[Dependency]  # annotated edges to direct deps
    depth: int = 0    # BFS depth from changed files


@dataclass
class MetaModel:
    modules: dict[str, Module] = field(default_factory=dict)
    changed_module_ids: list[str] = field(default_factory=list)
    changed_symbol_names: list[str] = field(default_factory=list)
    caller_module_ids: list[str] = field(default_factory=list)
    caller_reasons: dict[str, str] = field(default_factory=dict)  # caller file → usage summary

    def add(self, module: Module) -> None:
        self.modules[module.id] = module

    def to_json(self) -> list[dict]:
        """Serialize the graph to a JSON-serializable list of module dicts."""
        changed_ids = set(self.changed_module_ids)
        caller_ids = set(self.caller_module_ids)
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
                if s.before_code is not None:
                    sym["before_code"] = s.before_code
                symbols.append(sym)
            deps = [
                {
                    "name": d.name,
                    "fqn": d.fqn,
                    "usage": d.usage,
                    "file_path": d.file_path,
                    "usage_summary": d.usage_summary,
                }
                for d in module.dependencies
            ]
            result.append({
                "id": module.id,
                "name": module.name,
                "lang": module.lang,
                "summary": module.summary,
                "depth": module.depth,
                "is_changed": module.id in changed_ids,
                "is_caller": module.id in caller_ids,
                "dependencies": deps,
                "symbols": symbols,
            })
        return result
