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
    capabilities: list[str] = field(default_factory=list)
    judge_output: JudgeOutput | None = None
    run_at: datetime = field(default_factory=datetime.utcnow)
    error: str | None = None
    pr_url: str | None = None
    # Populated only on aggregated results (--repeat > 1). Each entry is the
    # individual ScenarioResult of one attempt; the wrapper carries the
    # median score and union of warnings.
    attempts: list["ScenarioResult"] = field(default_factory=list)
    score_min: float | None = None
    score_max: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def aggregate_results(attempts: list[ScenarioResult]) -> ScenarioResult:
    """Combine N per-attempt results into a single ScenarioResult.

    Score is the median over attempts. Verdict is "pass" when at least
    half of the attempts passed. Comments / duration use the median
    too. Agent and scenario warnings are unioned (deduped by kind+detail).
    The original per-attempt results are preserved on `.attempts` for
    drill-down.
    """
    assert attempts, "aggregate_results: empty attempts list"
    if len(attempts) == 1:
        return attempts[0]

    import statistics

    scores = sorted(r.score for r in attempts)
    median_score = statistics.median(scores)
    n_pass = sum(1 for r in attempts if r.verdict == "pass")
    has_error = any(r.verdict == "error" for r in attempts)
    if has_error and n_pass == 0:
        verdict = "error"
    else:
        verdict = "pass" if n_pass * 2 >= len(attempts) else "fail"

    median_comments = int(statistics.median(r.total_comments for r in attempts))
    median_duration = statistics.median(r.duration_seconds for r in attempts)
    median_required = int(statistics.median(r.required_found for r in attempts))
    median_fp = int(statistics.median(r.false_positives for r in attempts))

    base = attempts[0]

    seen_sw = set()
    seen_aw = set()
    union_judge = None
    if base.judge_output is not None:
        from runner.judge import JudgeOutput, ScenarioWarning, AgentWarning
        union_sw: list[ScenarioWarning] = []
        union_aw: list[AgentWarning] = []
        for r in attempts:
            jo = r.judge_output
            if jo is None:
                continue
            for w in jo.scenario_warnings:
                key = (w.kind, w.detail.strip())
                if key in seen_sw:
                    continue
                seen_sw.add(key)
                union_sw.append(w)
            for w in jo.agent_warnings:
                key = (w.kind, w.detail.strip())
                if key in seen_aw:
                    continue
                seen_aw.add(key)
                union_aw.append(w)
        union_judge = JudgeOutput(
            overall_score=median_score,
            required_comments=base.judge_output.required_comments,
            false_positives=base.judge_output.false_positives,
            status_change_verdict=base.judge_output.status_change_verdict,
            verdict=verdict,
            summary=f"median over {len(attempts)} attempts: {base.judge_output.summary}",
            scenario_warnings=union_sw,
            agent_warnings=union_aw,
        )

    return ScenarioResult(
        scenario_id=base.scenario_id,
        scenario_name=base.scenario_name,
        verdict=verdict,
        score=median_score,
        required_found=median_required,
        required_total=base.required_total,
        false_positives=median_fp,
        location_accuracy=statistics.mean(r.location_accuracy for r in attempts),
        status_change_verdict=base.status_change_verdict,
        inline_ratio=statistics.mean(r.inline_ratio for r in attempts),
        total_comments=median_comments,
        duration_seconds=median_duration,
        judge_summary=f"median over {len(attempts)} attempts",
        capabilities=base.capabilities,
        judge_output=union_judge,
        error=None,
        pr_url=base.pr_url,
        attempts=list(attempts),
        score_min=min(r.score for r in attempts),
        score_max=max(r.score for r in attempts),
    )


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

    # Interaction scenarios (/ask /help): there are no required_comments,
    # so min_required_found doesn't apply. Use score+verdict only.
    is_interaction = scenario.expected_output.reply is not None
    if is_interaction:
        passed = (
            judge_output.overall_score >= thresholds.min_score
            and judge_output.verdict == "pass"
        )
    else:
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
        capabilities=scenario.metadata.capabilities,
        judge_output=judge_output,
    )
