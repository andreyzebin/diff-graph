"""
Agent trigger mechanisms — execute agent for a PR.

Placeholders available in command templates:
  {pr_url}      — full PR URL
  {pr_id}       — PR number
  {project}     — Bitbucket project key
  {repo}        — repository slug
  {command}     — command name (review, ask, improve, ...)
  {args}        — command arguments (question text, instructions)
  {comment_id}  — parent comment ID (for threaded replies), empty if none
"""
from __future__ import annotations

import asyncio
import logging
import os

from .config import AgentConfig
from .bitbucket import PRMeta, CommandRequest

log = logging.getLogger(__name__)


async def trigger_agent(agent: AgentConfig, pr: PRMeta, cmd: CommandRequest) -> str:
    """
    Trigger an agent for a PR command. Returns output summary.
    """
    if agent.trigger == "cli":
        return await _trigger_cli(agent, pr, cmd)
    elif agent.trigger == "http":
        return await _trigger_http(agent, pr, cmd)
    else:
        raise ValueError(f"unknown trigger type: {agent.trigger}")


def _build_placeholders(pr: PRMeta, cmd: CommandRequest) -> dict[str, str]:
    """Build placeholder dict for command template substitution."""
    return {
        "pr_url": pr.pr_url,
        "pr_id": str(pr.pr_id),
        "project": pr.project,
        "repo": pr.repo,
        "command": cmd.name,
        "args": cmd.args or "",
        "comment_id": str(cmd.comment_id) if cmd.comment_id else "",
    }


async def _trigger_cli(agent: AgentConfig, pr: PRMeta, cmd: CommandRequest) -> str:
    """Run agent via shell command."""
    placeholders = _build_placeholders(pr, cmd)

    # Use per-command template if available, else default
    template = agent.commands.get(cmd.name, agent.command)
    try:
        command = template.format(**placeholders)
    except KeyError as e:
        return f"agent {agent.name} template error: missing placeholder {e}"

    log.info("trigger cli [%s]: %s", cmd.name, command[:200])

    proc = await asyncio.create_subprocess_shell(
        command,
        executable="/bin/bash",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.path.expanduser("~/"),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=agent.timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"agent {agent.name} timed out after {agent.timeout}s"
        log.error(msg)
        return msg

    if proc.returncode != 0:
        err_lines = stderr.decode(errors="replace").strip().splitlines()[-10:]
        msg = f"agent {agent.name} failed (exit {proc.returncode}): {' | '.join(err_lines)}"
        log.error(msg)
        return msg

    return f"agent {agent.name} completed (exit 0)"


async def _trigger_http(agent: AgentConfig, pr: PRMeta, cmd: CommandRequest) -> str:
    """Trigger agent via HTTP POST."""
    import urllib.request
    import json

    url = f"{agent.base_url.rstrip('/')}/{cmd.name}"
    payload = json.dumps({
        "pr_url": pr.pr_url,
        "pr_id": pr.pr_id,
        "project": pr.project,
        "repo": pr.repo,
        "command": cmd.name,
        "args": cmd.args,
        "comment_id": cmd.comment_id,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if agent.api_key:
        headers["Authorization"] = f"Bearer {agent.api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=agent.timeout) as resp:
            return f"agent {agent.name} responded {resp.status}"
    except Exception as exc:
        msg = f"agent {agent.name} http error: {exc}"
        log.error(msg)
        return msg
