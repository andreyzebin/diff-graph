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


@dataclass
class CapabilityRegression:
    capability: str
    old_score: float
    new_score: float
    diff: float
    diff_pct: float
    regressed: bool


def compare_capabilities(
    old_results: list,
    new_results: list,
    threshold_pct: float = -10.0,
) -> list[CapabilityRegression]:
    """Compare capability scores between two runs.

    Returns per-capability diff. regressed=True if diff_pct < threshold_pct.
    """
    old_caps = {c.capability: c for c in aggregate_by_capability(old_results)}
    new_caps = {c.capability: c for c in aggregate_by_capability(new_results)}
    all_caps = sorted(set(old_caps) | set(new_caps))

    regressions = []
    for cap in all_caps:
        old_score = old_caps[cap].avg_score if cap in old_caps else 0.0
        new_score = new_caps[cap].avg_score if cap in new_caps else 0.0
        diff = new_score - old_score
        diff_pct = (diff / old_score * 100) if old_score > 0 else 0.0
        regressions.append(CapabilityRegression(
            capability=cap,
            old_score=round(old_score, 3),
            new_score=round(new_score, 3),
            diff=round(diff, 3),
            diff_pct=round(diff_pct, 1),
            regressed=diff_pct < threshold_pct,
        ))
    return regressions
