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
