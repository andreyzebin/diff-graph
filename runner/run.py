from __future__ import annotations

import time

from fake_servers.context import FakeServersContext
from fake_servers.providers.factory import build_bitbucket_provider, build_jira_provider
from fake_servers.providers.base import FileContent
from runner.scenario_loader import Scenario
from runner.agent_client import AgentClient
from runner.judge import Judge
from runner.scorer import ScenarioResult, score_scenario


async def run_scenario(
    scenario: Scenario,
    agent_url: str,
    agent_client: AgentClient,
    judge: Judge,
    dry_run: bool = False,
) -> ScenarioResult:
    bb_provider = build_bitbucket_provider(scenario.input["bitbucket"])
    jira_provider = build_jira_provider(scenario.input["jira"])

    if dry_run:
        return ScenarioResult(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            verdict="dry_run",
            score=0.0,
            required_found=0,
            required_total=len(scenario.expected_output.required_comments),
            false_positives=0,
            location_accuracy=0.0,
            status_change_verdict="n/a",
            inline_ratio=0.0,
            total_comments=0,
            duration_seconds=0.0,
            judge_summary="dry run — agent not called",
        )

    start = time.monotonic()

    try:
        async with FakeServersContext(bb_provider, jira_provider) as servers:
            async with bb_provider.pr_lifecycle(scenario):
                pr_id = bb_provider.current_pr_id
                jira_cfg = scenario.input.get("jira", {})
                jira_key = (
                    jira_cfg.get("issue_key")
                    or jira_cfg.get("data", {}).get("issue", {}).get("key", "")
                )

                try:
                    await agent_client.run(
                        agent_url=agent_url,
                        pr_id=pr_id,
                        jira_key=jira_key,
                        bitbucket_url=servers.bitbucket_url,
                        jira_url=servers.jira_url,
                    )
                except Exception as e:
                    duration = time.monotonic() - start
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
                        duration_seconds=duration,
                        judge_summary=f"Agent error: {e}",
                        error=str(e),
                    )

                captured = await servers.get_captured()
    except Exception as e:
        duration = time.monotonic() - start
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
            judge_summary=f"Infrastructure error: {e}",
            error=str(e),
        )

    duration = time.monotonic() - start

    diff = await bb_provider.get_diff("BENCH", "test-repo", pr_id)
    jira_issue = None
    if jira_key:
        try:
            jira_issue = await jira_provider.get_issue(jira_key)
        except Exception:
            pass

    codebase_context = []
    for c in scenario.input.get("bitbucket", {}).get("data", {}).get("codebase_context", []):
        codebase_context.append(FileContent(path=c["path"], content=c["content"]))

    judge_output = await judge.evaluate(
        scenario=scenario,
        captured=captured,
        diff=diff,
        codebase_context=codebase_context,
        jira_summary=jira_issue.summary if jira_issue else "",
        jira_description=jira_issue.description if jira_issue else "",
    )

    return score_scenario(scenario, captured, judge_output, duration)
