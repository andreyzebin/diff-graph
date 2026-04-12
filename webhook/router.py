"""
Route webhook events to agents.

Two routing modes per route:
- forward: send raw event to agent (event-level, no command extraction)
- agent: extract commands, route per-command (command-level)

sample= controls what percentage of PRs a route matches (by pr_url hash).
Routes evaluated top to bottom, first match wins. Unmatched PRs fall through.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from .config import WebhookConfig, Route
from .bitbucket import PRMeta, CommandRequest

log = logging.getLogger(__name__)


@dataclass
class ForwardDecision:
    """Event-level: forward raw event to this agent."""
    agent_name: str
    route_name: str


@dataclass
class CommandDecision:
    """Command-level: run this command on this agent."""
    command: CommandRequest
    agent_name: str
    route_name: str


def route_event(
    commands: list[CommandRequest],
    pr: PRMeta,
    config: WebhookConfig,
) -> ForwardDecision | list[CommandDecision]:
    """
    Route an event through the route table.

    Returns either:
    - ForwardDecision (event-level: forward raw event)
    - list[CommandDecision] (command-level: per-command routing)
    """
    ctx = _build_ctx(pr)

    for route in config.routes:
        if not _eval_when(route.when, ctx):
            continue
        if not _sample_match(route.sample, pr.pr_url):
            continue

        # Event-level: forward
        if route.forward:
            if route.forward in config.agents:
                log.info('PR #%s %s/%s → route "%s" → forward:%s',
                         pr.pr_id, pr.project, pr.repo,
                         route.name, route.forward)
                return ForwardDecision(
                    agent_name=route.forward,
                    route_name=route.name,
                )
            continue

        # Command-level: route each command
        if route.agent is not None:
            decisions = []
            for cmd in commands:
                agent_name = route.commands.get(cmd.name, route.agent)
                if agent_name and agent_name in config.agents:
                    log.info('PR #%s %s/%s → route "%s" → %s:%s',
                             pr.pr_id, pr.project, pr.repo,
                             route.name, cmd.name, agent_name)
                    decisions.append(CommandDecision(
                        command=cmd,
                        agent_name=agent_name,
                        route_name=route.name,
                    ))
            if decisions:
                return decisions

    log.warning("no route matched for PR #%s %s/%s", pr.pr_id, pr.project, pr.repo)
    return []


def _build_ctx(pr: PRMeta) -> dict:
    return {
        "project": pr.project,
        "repo": pr.repo,
        "author": pr.author,
        "branch": pr.branch,
        "target": pr.target,
        "pr_url": pr.pr_url,
        "pr_id": pr.pr_id,
        "title": pr.title,
    }


def _eval_when(expr: str, ctx: dict) -> bool:
    """Safely evaluate a when expression against PR metadata."""
    if expr in ("true", "True", "*"):
        return True
    try:
        return bool(eval(expr, {"__builtins__": {}}, ctx))
    except Exception as exc:
        log.warning("route eval failed: %s — %s", expr, exc)
        return False


def _sample_match(sample: int, pr_url: str) -> bool:
    """Check if this PR falls within the sample percentage."""
    if sample >= 100:
        return True
    if sample <= 0:
        return False
    h = int(hashlib.md5(pr_url.encode()).hexdigest(), 16) % 100
    return h < sample
