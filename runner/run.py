from __future__ import annotations

import time

from bitbucket.base import BitbucketPRProxy, DiffHunk, FileDiff, FileContent
from runner.scenario_loader import Scenario
from runner.agent_client import AgentClient
from runner.judge import Judge
from runner.scorer import ScenarioResult, score_scenario


def _parse_diff(data: list[dict]) -> list[FileDiff]:
    diffs = []
    for fd in data:
        hunks = [
            DiffHunk(
                old_start=h["old_start"],
                new_start=h["new_start"],
                lines=h["lines"],
            )
            for h in fd.get("hunks", [])
        ]
        diffs.append(FileDiff(
            path=fd["path"],
            change_type=fd.get("change_type", "MODIFY"),
            hunks=hunks,
        ))
    return diffs


async def run_scenario(
    scenario: Scenario,
    proxy: BitbucketPRProxy,
    agent_client: AgentClient,
    judge: Judge,
) -> ScenarioResult:
    bb_data = scenario.input.get("bitbucket", {}).get("data", {})
    jira_issue = scenario.input.get("jira", {}).get("data", {}).get("issue", {})

    diff = _parse_diff(bb_data.get("diff", []))
    codebase_context = [
        FileContent(path=f["path"], content=f["content"])
        for f in bb_data.get("codebase_context", [])
    ]
    jira_key = jira_issue.get("key") or None
    jira_summary = jira_issue.get("summary", "")
    jira_description = jira_issue.get("description", "")

    start = time.monotonic()

    try:
        await agent_client.run(pr_id=proxy.pr_id, jira_key=jira_key)
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
        )

    comments = await proxy.get_comments()
    review_status = await proxy.get_review_status()
    duration = time.monotonic() - start

    judge_output = await judge.evaluate(
        scenario=scenario,
        comments=comments,
        review_status=review_status,
        diff=diff,
        codebase_context=codebase_context,
        jira_summary=jira_summary,
        jira_description=jira_description,
    )

    return score_scenario(scenario, comments, review_status, judge_output, duration)
