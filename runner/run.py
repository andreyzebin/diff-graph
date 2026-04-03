from __future__ import annotations

import time

from bitbucket.base import AgentPRView
from runner.scenario_loader import Scenario
from runner.trigger import Trigger
from runner.judge import Judge
from runner.scorer import ScenarioResult, score_scenario


async def run_scenario(
    scenario: Scenario,
    proxy: AgentPRView,
    trigger: Trigger,
    judge: Judge,
) -> ScenarioResult:
    start = time.monotonic()

    try:
        await trigger.activate(proxy)
    except Exception as e:
        return ScenarioResult(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            verdict="error",
            score=0.0,
            required_found=0,
            required_total=len(scenario.expected_output.required_comments),
            false_positives=0,
            location_accuracy=0.0,
            status_change_verdict="n/a",
            inline_ratio=0.0,
            total_comments=0,
            duration_seconds=time.monotonic() - start,
            judge_summary=f"Agent error: {e}",
            error=str(e),
            pr_url=proxy.pr_url,
        )

    judge_output = await judge.evaluate(scenario=scenario)
    duration = time.monotonic() - start

    result = score_scenario(
        scenario,
        judge_output.comments,
        judge_output.review_status,
        judge_output,
        duration,
    )
    result.pr_url = proxy.pr_url
    return result
