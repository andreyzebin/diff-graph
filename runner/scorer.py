from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from bitbucket.base import CommentThread, ReviewStatus
from runner.judge import JudgeOutput
from runner.scenario_loader import Scenario


@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_name: str
    verdict: str          # pass | fail | error | dry_run
    score: float
    required_found: int
    required_total: int
    false_positives: int
    location_accuracy: float
    status_change_verdict: str
    inline_ratio: float
    total_comments: int
    duration_seconds: float
    judge_summary: str
    judge_output: JudgeOutput | None = None
    run_at: datetime = field(default_factory=datetime.utcnow)
    error: str | None = None
    pr_url: str | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def score_scenario(
    scenario: Scenario,
    comments: list[CommentThread],
    review_status: ReviewStatus | None,
    judge_output: JudgeOutput,
    duration_seconds: float,
) -> ScenarioResult:
    thresholds = scenario.expected_output.thresholds

    required_found = sum(1 for rc in judge_output.required_comments if rc.found)
    required_total = len(judge_output.required_comments)

    accurate = [rc for rc in judge_output.required_comments if rc.found and rc.location_accurate]
    location_accuracy = len(accurate) / required_found if required_found > 0 else 0.0

    inline_count = sum(1 for c in comments if c.anchor is not None)
    total_count = len(comments)
    inline_ratio = inline_count / total_count if total_count > 0 else 0.0

    false_positives = len(judge_output.false_positives)

    passed = (
        judge_output.overall_score >= thresholds.min_score
        and required_found >= thresholds.min_required_found
        and false_positives <= thresholds.max_false_positives
    )

    return ScenarioResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        verdict="pass" if passed else "fail",
        score=judge_output.overall_score,
        required_found=required_found,
        required_total=required_total,
        false_positives=false_positives,
        location_accuracy=location_accuracy,
        status_change_verdict=judge_output.status_change_verdict,
        inline_ratio=inline_ratio,
        total_comments=total_count,
        duration_seconds=duration_seconds,
        judge_summary=judge_output.summary,
        judge_output=judge_output,
    )
