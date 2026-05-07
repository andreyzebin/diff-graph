from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bitbucket.base import AgentPRView
from runner.scenario_loader import Scenario
from runner.trigger import Trigger
from runner.judge import Judge
from runner.scorer import ScenarioResult, score_scenario

log = logging.getLogger(__name__)


def _walk_seed(nodes):
    """DFS-yield every node in a SeedComment tree (for counting)."""
    for n in nodes:
        yield n
        yield from _walk_seed(n.replies)


def _resolve_path(posted_tree: list[dict], path: list[int]) -> int | None:
    """Walk the posted-tree by integer indices and return the leaf comment id.

    Each entry: {"id": <bitbucket id>, "replies": [...same shape...]}.
    Returns None if any index is out of range.
    """
    cur = posted_tree
    cid = None
    for idx in path:
        if not isinstance(idx, int) or idx < 0 or idx >= len(cur):
            return None
        node = cur[idx]
        cid = node["id"]
        cur = node["replies"]
    return cid


def _shell_quote(s: str) -> str:
    """Single-quote a value for safe substitution into a bash command line."""
    if not s:
        return "''"
    if "'" not in s:
        return f"'{s}'"
    return "'" + s.replace("'", "'\\''") + "'"


def _build_extra_args(scenario: Scenario, invocations_out: Path | None = None) -> list[str]:
    """Compose the --mocks / --agent / -d / --invocations-out /
    --user-message[-from] flags from agent-isolation scenario fields.
    Returns an empty list when none of them are set (scenario behaves
    exactly as before)."""
    args: list[str] = []
    if scenario.trigger.agent:
        args.append(f"--agent={_shell_quote(scenario.trigger.agent)}")
    for k, v in (scenario.trigger.data or {}).items():
        args.append(f"-d {_shell_quote(f'{k}={v}')}")
    if scenario.setup.mocks_path:
        args.append(f"--mocks={_shell_quote(str(scenario.setup.mocks_path))}")
    if scenario.trigger.user_message_path:
        args.append(f"--user-message-from={_shell_quote(str(scenario.trigger.user_message_path))}")
    elif scenario.trigger.user_message:
        args.append(f"--user-message={_shell_quote(scenario.trigger.user_message)}")
    if invocations_out is not None:
        args.append(f"--invocations-out={_shell_quote(str(invocations_out))}")
    return args


async def _seed_and_trigger(scenario: Scenario, proxy: AgentPRView, trigger: Trigger,
                            env_overrides: dict | None = None,
                            extra_args: list[str] | None = None) -> list[int]:
    """
    Apply scenario.setup.seed_comments, then fire the trigger.

    Returns the list of comment ids the runner itself posted (seed
    comments + trigger comment) so the judge can exclude them from the
    agent's replies — with a single Bitbucket account everything is
    authored by the same user, and without this filter the judge sees
    `/help` (the trigger) and concludes the agent "merely echoed".

    For trigger.type == "comment": seed comments are posted in Bitbucket
    so the agent has thread context, then the trigger.text is posted as
    the LAST comment (capturing its id), and the configured trigger
    runs the agent CLI locally with --message=trigger.text and
    --comment-id=<id>. The agent replies via its own Bitbucket session.

    For trigger.type == "auto" (default): trigger fires as before (e.g.
    CliTrigger with the review command, or WebhookTrigger).
    """
    posted_ids: list[int] = []
    # Mirrors scenario.setup.seed_comments shape: each tree node carries
    # the Bitbucket comment id assigned when it was posted, so the
    # trigger.in_reply_to path can be resolved.
    posted_tree: list[dict] = []

    async def _post_tree(nodes, parent_id, posted_into: list[dict], depth=0):
        for n in nodes:
            try:
                cid = await proxy.add_comment(n.text, parent_id=parent_id)
            except NotImplementedError:
                log.warning("scenario %s: proxy does not support add_comment",
                            scenario.id)
                return
            except Exception as exc:
                log.warning("scenario %s: seed_comment failed: %s",
                            scenario.id, exc)
                continue
            if cid:
                posted_ids.append(cid)
            posted_into.append({"id": cid, "replies": []})
            indent = "    " * depth
            print(f"   ↻  {indent}seeded #{cid}: {n.text[:60]}{'…' if len(n.text) > 60 else ''}",
                  flush=True)
            await asyncio.sleep(0.5)
            if n.replies:
                await _post_tree(n.replies, cid, posted_into[-1]["replies"], depth + 1)

    if scenario.setup.seed_comments:
        n_total = sum(1 for _ in _walk_seed(scenario.setup.seed_comments))
        print(f"   ↻  posting {n_total} seed comment(s) (tree)...", flush=True)
        await _post_tree(scenario.setup.seed_comments, None, posted_tree)

    if scenario.trigger.type == "comment" and scenario.trigger.text:
        # Resolve in_reply_to path against the just-posted tree so the
        # trigger comment lands at exactly the right depth.
        parent_id = None
        if scenario.trigger.in_reply_to:
            parent_id = _resolve_path(posted_tree, scenario.trigger.in_reply_to)
            if parent_id is None:
                raise RuntimeError(
                    f"trigger.in_reply_to path {scenario.trigger.in_reply_to} "
                    f"didn't resolve against the posted seed tree"
                )
            print(f"   ↻  trigger comment (reply to #{parent_id}): "
                  f"{scenario.trigger.text[:80]}", flush=True)
        else:
            print(f"   ↻  trigger comment: {scenario.trigger.text[:80]}", flush=True)
        try:
            comment_id = await proxy.add_comment(scenario.trigger.text,
                                                 parent_id=parent_id)
        except Exception as exc:
            raise RuntimeError(f"trigger comment failed: {exc}") from exc
        if not comment_id:
            raise RuntimeError("trigger comment returned id=0 — can't drive agent")
        posted_ids.append(comment_id)

        # Run the agent CLI locally with the dispatcher inputs. The
        # configured CliTrigger command must accept {message} and
        # {comment_id} placeholders for interaction scenarios — see the
        # benchmark config.local.yaml example.
        try:
            await trigger.activate(
                proxy,
                env_overrides=env_overrides,
                extra_args=extra_args,
                message=scenario.trigger.text,
                comment_id=str(comment_id),
            )
        except TypeError:
            # Older trigger that doesn't accept extra placeholders
            raise RuntimeError(
                "configured trigger does not support interaction scenarios; "
                "update CliTrigger or pin to one that does"
            )
        return posted_ids

    # Default path: existing trigger strategies (http / webhook / cli)
    try:
        await trigger.activate(proxy, env_overrides=env_overrides, extra_args=extra_args)
    except TypeError:
        # Older Trigger subclass without env_overrides / extra_args.
        try:
            await trigger.activate(proxy, env_overrides=env_overrides)
        except TypeError:
            await trigger.activate(proxy)
    return posted_ids


async def run_scenario(
    scenario: Scenario,
    proxy: AgentPRView,
    trigger: Trigger,
    judge: Judge,
    env_overrides: dict | None = None,
    invocations_out: Path | None = None,
) -> ScenarioResult:
    start = time.monotonic()

    extra_args = _build_extra_args(scenario, invocations_out=invocations_out)

    posted_ids: list[int] = []
    try:
        posted_ids = await _seed_and_trigger(
            scenario, proxy, trigger,
            env_overrides=env_overrides, extra_args=extra_args,
        )
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

    judge_output = await judge.evaluate(scenario=scenario, exclude_comment_ids=set(posted_ids))
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
