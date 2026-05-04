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

    For trigger.type == "comment": seed comments are posted in Bitbucket
    so the agent has thread context, then the trigger.text is posted as
    the LAST comment (capturing its id), and the configured trigger
    runs the agent CLI locally with --message=trigger.text and
    --comment-id=<id>. The agent replies via its own Bitbucket session.

    For trigger.type == "auto" (default): trigger fires as before (e.g.
    CliTrigger with the review command, or WebhookTrigger).
    """
    n_seeds = len(scenario.setup.seed_comments)
    if n_seeds:
        print(f"   ↻  posting {n_seeds} seed comment(s)...", flush=True)
    for i, body in enumerate(scenario.setup.seed_comments, start=1):
        try:
            await proxy.add_comment(body)
            print(f"   ↻  [{i}/{n_seeds}] seeded: {body[:60]}{'…' if len(body) > 60 else ''}", flush=True)
            await asyncio.sleep(0.5)   # space out so order is preserved
        except NotImplementedError:
            log.warning("scenario %s: proxy does not support add_comment, "
                        "seed_comments ignored", scenario.id)
            break
        except Exception as exc:
            log.warning("scenario %s: seed_comment failed: %s", scenario.id, exc)

    if scenario.trigger.type == "comment" and scenario.trigger.text:
        print(f"   ↻  trigger comment: {scenario.trigger.text[:80]}", flush=True)
        try:
            comment_id = await proxy.add_comment(scenario.trigger.text)
        except Exception as exc:
            raise RuntimeError(f"trigger comment failed: {exc}") from exc
        if not comment_id:
            raise RuntimeError("trigger comment returned id=0 — can't drive agent")

        # Run the agent CLI locally with the dispatcher inputs. The
        # configured CliTrigger command must accept {message} and
        # {comment_id} placeholders for interaction scenarios — see the
        # benchmark config.local.yaml example.
        try:
            await trigger.activate(
                proxy,
                message=scenario.trigger.text,
                comment_id=str(comment_id),
            )
        except TypeError:
            # Older trigger that doesn't accept extra placeholders
            raise RuntimeError(
                "configured trigger does not support interaction scenarios; "
                "update CliTrigger or pin to one that does"
            )
        return

    # Default path: existing trigger strategies (http / webhook / cli)
    await trigger.activate(proxy)


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
