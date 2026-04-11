"""
Route commands to agents based on TOML config rules.

Supports:
- Exact agent assignment: agent = "dg2"
- A/B split: agent = { dg2 = 30, dg1 = 70 } — deterministic by pr_url hash
- Per-command overrides: review = "dg2", improve = "pra"
- Default via `agent` key + per-command override
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from .config import WebhookConfig, Route
from .bitbucket import PRMeta, CommandRequest

log = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    command: CommandRequest
    agent_name: str
    route_name: str


def route_commands(
    commands: list[CommandRequest],
    pr: PRMeta,
    config: WebhookConfig,
) -> list[RoutingDecision]:
    """
    Route each command to an agent based on config rules.

    Returns a RoutingDecision per command. Commands with no matching
    route are dropped (logged as warning).
    """
    decisions = []

    for cmd in commands:
        decision = _route_single(cmd, pr, config)
        if decision:
            decisions.append(decision)
        else:
            log.warning("no route for command=%s project=%s repo=%s",
                        cmd.name, pr.project, pr.repo)

    return decisions


def _route_single(
    cmd: CommandRequest, pr: PRMeta, config: WebhookConfig,
) -> RoutingDecision | None:
    """Find first matching route for a command."""
    ctx = {
        "project": pr.project,
        "repo": pr.repo,
        "author": pr.author,
        "branch": pr.branch,
        "target": pr.target,
        "pr_url": pr.pr_url,
        "pr_id": pr.pr_id,
        "title": pr.title,
    }

    for route in config.routes:
        if not _eval_when(route.when, ctx):
            continue

        # Per-command override takes priority
        agent_spec = route.commands.get(cmd.name)

        # Fall back to default agent
        if agent_spec is None:
            agent_spec = route.agent

        if agent_spec is None:
            continue  # route matches but has no agent for this command

        agent_name = _resolve_agent(agent_spec, pr.pr_url)

        if agent_name and agent_name in config.agents:
            log.info('PR #%s %s/%s → route "%s" → %s:%s',
                     pr.pr_id, pr.project, pr.repo,
                     route.name, cmd.name, agent_name)
            return RoutingDecision(
                command=cmd,
                agent_name=agent_name,
                route_name=route.name,
            )

    return None


def _eval_when(expr: str, ctx: dict) -> bool:
    """Safely evaluate a when expression against PR metadata."""
    if expr in ("true", "True", "*"):
        return True
    try:
        return bool(eval(expr, {"__builtins__": {}}, ctx))
    except Exception as exc:
        log.warning("route eval failed: %s — %s", expr, exc)
        return False


def _resolve_agent(spec: str | dict, pr_url: str) -> str | None:
    """
    Resolve agent from spec.

    - str: exact agent name
    - dict: A/B split {agent_name: percentage}, deterministic by pr_url hash
    """
    if isinstance(spec, str):
        return spec

    if isinstance(spec, dict):
        # Deterministic hash: same PR always gets same agent
        h = int(hashlib.md5(pr_url.encode()).hexdigest(), 16) % 100
        cumulative = 0
        for agent_name, pct in spec.items():
            cumulative += pct
            if h < cumulative:
                return agent_name
        # Fallback to last
        return list(spec.keys())[-1] if spec else None

    return None
