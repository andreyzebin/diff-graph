"""
Population manager — branches, measurements, fitness, tick().
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Branch, Status, Measurement, EvolutionConfig
from .connectors import TracingConnector, AnalyticsConnector, BenchmarkConnector, WebhookConnector

log = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".diffgraph" / "evolution.json"


class Population:
    """Manages the population of prompt branches."""

    def __init__(
        self,
        config: EvolutionConfig = None,
        tracing: TracingConnector = None,
        analytics: AnalyticsConnector = None,
        benchmark: BenchmarkConnector = None,
        webhook: WebhookConnector = None,
        state_file: Path = STATE_FILE,
    ):
        self.config = config or EvolutionConfig()
        self.tracing = tracing or TracingConnector()
        self.analytics = analytics or AnalyticsConnector()
        self.benchmark = benchmark or BenchmarkConnector()
        self.webhook = webhook or WebhookConnector()
        self.state_file = state_file
        self.branches: dict[str, Branch] = {}
        self.measurements: dict[str, Measurement] = {}
        self._load_state()

    # ── Branch management ─────────────────────────────────────

    def add_branch(self, branch: Branch) -> None:
        self.branches[branch.id] = branch
        self._save_state()

    def get_branch(self, branch_id: str) -> Optional[Branch]:
        return self.branches.get(branch_id)

    def active_branches(self) -> list[Branch]:
        return [b for b in self.branches.values() if b.status in (Status.ACTIVE, Status.DOMINANT)]

    def init_main(self, prompt_ref: str, prompt_hash: str) -> Branch:
        """Initialize or update main branch."""
        main = Branch(
            id="main",
            prompt_ref=prompt_ref,
            prompt_hash=prompt_hash,
            status=Status.ACTIVE,
            sample_pct=100.0,
            generation=0,
            created_at=datetime.now().isoformat(),
        )
        self.branches["main"] = main
        self._save_state()
        return main

    # ── Measurement ───────────────────────────────────────────

    def measure(self, branch_id: str) -> Measurement:
        """Collect metrics from all sources for a branch."""
        branch = self.branches.get(branch_id)
        if not branch:
            return Measurement(branch_id=branch_id, timestamp=datetime.now().isoformat())

        m = Measurement(
            branch_id=branch_id,
            timestamp=datetime.now().isoformat(),
        )

        # Tracing
        try:
            t = self.tracing.metrics(branch.prompt_hash)
            m.runs_count = t.get("runs_count", 0)
            m.tokens_per_finding = t.get("tokens_per_finding", 0)
            m.steps_avg = t.get("steps_avg", 0)
            m.cache_ratio = t.get("cache_ratio", 0)
        except Exception as exc:
            log.warning("tracing metrics failed for %s: %s", branch_id, exc)

        # Analytics
        try:
            a = self.analytics.acceptance(branch.prompt_hash)
            m.acceptance_rate = a.get("acceptance_rate")
            m.false_positive_rate = a.get("false_positive_rate")
            m.feedback_rate = a.get("feedback_rate")
        except Exception as exc:
            log.warning("analytics failed for %s: %s", branch_id, exc)

        # Fitness
        m.fitness = self._compute_fitness(m)
        branch.fitness = m.fitness
        self.measurements[branch_id] = m
        self._save_state()
        return m

    def measure_all(self) -> dict[str, Measurement]:
        """Measure all active branches."""
        results = {}
        for branch in self.active_branches():
            results[branch.id] = self.measure(branch.id)
        return results

    def run_benchmark(self, branch_id: str) -> dict:
        """Run benchmark suite for a branch."""
        branch = self.branches.get(branch_id)
        if not branch:
            return {"error": f"branch {branch_id} not found"}

        result = self.benchmark.run(prompts_uri=branch.prompt_ref)

        if branch.status == Status.BORN:
            branch.status = Status.BENCHMARKED
            self._save_state()

        # Store benchmark score in measurement
        m = self.measurements.get(branch_id) or Measurement(
            branch_id=branch_id, timestamp=datetime.now().isoformat()
        )
        m.benchmark_score = result.get("overall_score")
        m.by_capability = result.get("by_capability", {})
        m.regressions = len(result.get("regressions", []))
        m.fitness = self._compute_fitness(m)
        branch.fitness = m.fitness
        self.measurements[branch_id] = m
        self._save_state()

        return result

    # ── Fitness ───────────────────────────────────────────────

    def _compute_fitness(self, m: Measurement) -> float:
        c = self.config
        f = 0.0

        if m.benchmark_score is not None:
            f += c.w_benchmark * m.benchmark_score

        if m.acceptance_rate is not None:
            f += c.w_acceptance * m.acceptance_rate

        if m.tokens_per_finding and m.tokens_per_finding > 0:
            # Normalize: 10000 tokens/finding = 0.0, 1000 = 1.0
            eff = max(0, 1.0 - m.tokens_per_finding / 10000)
            f += c.w_efficiency * eff

        if m.feedback_rate is not None:
            f += c.w_engagement * m.feedback_rate

        return round(f, 3)

    # ── Status ────────────────────────────────────────────────

    def status(self) -> dict:
        """Full population status."""
        return {
            "branches": [b.to_dict() for b in self.branches.values()],
            "measurements": {k: v.to_dict() for k, v in self.measurements.items()},
            "config": self.config.to_dict(),
        }

    def tree(self) -> str:
        """Phylogenetic tree visualization."""
        lines = []
        # Sort: main first, then by generation
        sorted_branches = sorted(
            self.branches.values(),
            key=lambda b: (b.generation, b.id),
        )
        for b in sorted_branches:
            indent = "  " * b.generation
            parent_info = f" ← {b.parent_id}" if b.parent_id else ""
            fitness_str = f"fitness={b.fitness:.3f}" if b.fitness else ""
            sample_str = f"{b.sample_pct:.0f}%"
            lines.append(
                f"{indent}{b.id}{parent_info}  "
                f"{sample_str}  {fitness_str}  {b.status.value}"
            )
        return "\n".join(lines) if lines else "(empty population)"

    # ── State persistence ─────────────────────────────────────

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "branches": {k: v.to_dict() for k, v in self.branches.items()},
            "measurements": {k: v.to_dict() for k, v in self.measurements.items()},
            "config": self.config.to_dict(),
        }
        self.state_file.write_text(json.dumps(state, indent=2))

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text())
            for k, d in state.get("branches", {}).items():
                self.branches[k] = Branch(
                    id=d["id"],
                    prompt_ref=d.get("prompt_ref", ""),
                    prompt_hash=d.get("prompt_hash", ""),
                    parent_id=d.get("parent_id"),
                    axis=d.get("axis", ""),
                    hypothesis=d.get("hypothesis", ""),
                    status=Status(d.get("status", "born")),
                    sample_pct=d.get("sample_pct", 0),
                    generation=d.get("generation", 0),
                    created_at=d.get("created_at", ""),
                    fitness=d.get("fitness", 0),
                )
            # Load config overrides
            cfg = state.get("config", {})
            for k, v in cfg.items():
                if hasattr(self.config, k):
                    setattr(self.config, k, v)
        except Exception as exc:
            log.warning("failed to load evolution state: %s", exc)
