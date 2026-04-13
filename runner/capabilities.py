"""
Aggregate benchmark results by capability.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class CapabilityScore:
    capability: str
    avg_score: float
    scenarios: int
    passed: int
    failed: int


def aggregate_by_capability(results: list) -> list[CapabilityScore]:
    """Aggregate ScenarioResult list by capability tag.

    Each scenario may have multiple capabilities. A scenario's score
    contributes to each of its capabilities.
    """
    cap_scores: dict[str, list[float]] = defaultdict(list)
    cap_passed: dict[str, int] = defaultdict(int)
    cap_failed: dict[str, int] = defaultdict(int)

    for r in results:
        caps = getattr(r, "capabilities", []) or []
        if not caps:
            caps = ["uncategorized"]
        for cap in caps:
            cap_scores[cap].append(r.score)
            if r.verdict == "pass":
                cap_passed[cap] += 1
            elif r.verdict == "fail":
                cap_failed[cap] += 1

    out = []
    for cap in sorted(cap_scores.keys()):
        scores = cap_scores[cap]
        out.append(CapabilityScore(
            capability=cap,
            avg_score=sum(scores) / len(scores) if scores else 0,
            scenarios=len(scores),
            passed=cap_passed.get(cap, 0),
            failed=cap_failed.get(cap, 0),
        ))
    return out


def weaknesses(results: list, threshold: float = 0.7) -> list[CapabilityScore]:
    """Return capabilities scoring below threshold, sorted worst first."""
    caps = aggregate_by_capability(results)
    return sorted(
        [c for c in caps if c.avg_score < threshold],
        key=lambda c: c.avg_score,
    )
