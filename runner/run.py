from __future__ import annotations

import asyncio
import logging
import time

from bitbucket.base import AgentPRView
from runner.scenario_loader import Scenario
from runner.trigger import Trigger
from runner.judge import Judge
from runner.scorer import ScenarioResult, score_scenario

log = logging.getLogger(__name__)


async def _seed_and_trigger(scenario: Scenario, proxy: AgentPRView, trigger: Trigger) -> None:
    """
    Apply scenario.setup.seed_comments, then fire the trigger.

    For trigger.type == "comment": seed comments are posted in order;
    the LAST seed_comment OR a freshly-posted trigger.text becomes the
    one that fires `pr:comment:added`. Webhook then drives the agent.

    For trigger.type == "auto" (default): seed comments still get posted
    if any (rare for auto-triggered scenarios), then the configured
    trigger (HttpTrigger / WebhookTrigger / CliTrigger) fires.
    """
    for body in scenario.setup.seed_comments:
        try:
            await proxy.add_comment(body)
            await asyncio.sleep(0.5)   # space out so order is preserved
        except NotImplementedError:
            log.warning("scenario %s: proxy does not support add_comment, "
                        "seed_comments ignored", scenario.id)
            break
        except Exception as exc:
            log.warning("scenario %s: seed_comment failed: %s", scenario.id, exc)

    if scenario.trigger.type == "comment" and scenario.trigger.text:
        # Posting the trigger comment fires `pr:comment:added` directly,
        # so for comment-mode scenarios we don't call trigger.activate().
        try:
            await proxy.add_comment(scenario.trigger.text)
        except Exception as exc:
            raise RuntimeError(f"trigger comment failed: {exc}") from exc
        # Give the webhook + agent time to work. Wait for the agent to
        # reply, with a budget; fall back to a fixed sleep on timeout.
        await _wait_for_agent_reply(proxy, timeout=600)
        return

    # Default path: existing trigger strategies (http / webhook / cli)
    await trigger.activate(proxy)


async def _wait_for_agent_reply(proxy: AgentPRView, timeout: int = 600) -> None:
    """Poll the PR for any agent-authored comment that didn't exist before."""
    deadline = time.monotonic() + timeout
    seen_pre = {c.id for c in await proxy.get_comments()}
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        try:
            now = await proxy.get_comments()
        except Exception:
            continue
        if any(c.id not in seen_pre for c in now):
            return
    log.warning("agent did not reply within %ds — proceeding to judge anyway", timeout)


async def run_scenario(
    scenario: Scenario,
    proxy: AgentPRView,
    trigger: Trigger,
    judge: Judge,
) -> ScenarioResult:
    start = time.monotonic()

    try:
        await _seed_and_trigger(scenario, proxy, trigger)
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
