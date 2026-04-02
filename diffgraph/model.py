from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


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
    dependencies: list[str]  # names: ["CardValidator", "OrderRepository"]
    depth: int = 0    # BFS depth at which this module was found


@dataclass
class MetaModel:
    modules: dict[str, Module] = field(default_factory=dict)
    changed_module_ids: list[str] = field(default_factory=list)
    changed_symbol_names: list[str] = field(default_factory=list)

    def add(self, module: Module) -> None:
        self.modules[module.id] = module

    def to_json(self) -> list[dict]:
        """Serialize the graph to a JSON-serializable list of module dicts."""
        changed_ids = set(self.changed_module_ids)
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
                if s.full_code is not None:
                    sym["full_code"] = s.full_code
                if s.before_code is not None:
                    sym["before_code"] = s.before_code
                symbols.append(sym)
            result.append({
                "id": module.id,
                "name": module.name,
                "lang": module.lang,
                "summary": module.summary,
                "depth": module.depth,
                "is_changed": module.id in changed_ids,
                "dependencies": module.dependencies,
                "symbols": symbols,
            })
        return result
