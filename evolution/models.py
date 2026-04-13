"""
Core entities: Branch, Population, EvolutionConfig, Measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Status(str, Enum):
    BORN = "born"
    BENCHMARKED = "benchmarked"
    ACTIVE = "active"
    DOMINANT = "dominant"
    MERGED = "merged"
    EXTINCT = "extinct"


@dataclass
class Branch:
    id: str                          # "main" or "mut-042-budget"
    prompt_ref: str                  # path or URI to prompts
    prompt_hash: str                 # content hash or commit SHA
    parent_id: Optional[str] = None  # None for main
    axis: str = ""                   # mutation axis
    hypothesis: str = ""             # what we're testing
    status: Status = Status.BORN
    sample_pct: float = 0.0          # current traffic allocation
    generation: int = 0              # distance from main
    created_at: str = ""
    fitness: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt_ref": self.prompt_ref,
            "prompt_hash": self.prompt_hash,
            "parent_id": self.parent_id,
            "axis": self.axis,
            "hypothesis": self.hypothesis,
            "status": self.status.value,
            "sample_pct": self.sample_pct,
            "generation": self.generation,
            "fitness": round(self.fitness, 3),
            "created_at": self.created_at,
        }


@dataclass
class Measurement:
    branch_id: str
    timestamp: str
    runs_count: int = 0
    # Business (from analytics)
    acceptance_rate: Optional[float] = None
    false_positive_rate: Optional[float] = None
    feedback_rate: Optional[float] = None
    # Quality (from benchmarks)
    benchmark_score: Optional[float] = None
    by_capability: dict[str, float] = field(default_factory=dict)
    regressions: int = 0
    # Efficiency (from tracing)
    tokens_per_finding: float = 0.0
    steps_avg: float = 0.0
    cache_ratio: float = 0.0
    # Composite
    fitness: float = 0.0

    def to_dict(self) -> dict:
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, float):
                d[k] = round(v, 3) if v else v
            else:
                d[k] = v
        return d


@dataclass
class EvolutionConfig:
    # Fitness weights
    w_benchmark: float = 0.35
    w_acceptance: float = 0.35
    w_efficiency: float = 0.20
    w_engagement: float = 0.10
    # Population
    min_main_pct: float = 30.0
    max_branches: int = 5
    min_runs_to_evaluate: int = 15
    # Breeding
    breed_threshold: float = 0.7
    max_children: int = 2
    # Extinction
    extinct_threshold: float = 0.3
    extinct_days: int = 14
    # Convergence
    converge_p_value: float = 0.01
    converge_days: int = 14

    def to_dict(self) -> dict:
        return self.__dict__.copy()
