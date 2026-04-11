"""
Agent trigger mechanisms — execute agent for a PR.
"""
from __future__ import annotations

import asyncio
import logging
import os

from .config import AgentConfig
from .bitbucket import PRMeta

log = logging.getLogger(__name__)


async def trigger_agent(agent: AgentConfig, pr: PRMeta, command: str) -> str:
    """
    Trigger an agent for a PR. Returns output summary.

    Dispatches to the appropriate trigger mechanism based on agent.trigger.
    """
    if agent.trigger == "cli":
        return await _trigger_cli(agent, pr)
    elif agent.trigger == "http":
        return await _trigger_http(agent, pr, command)
    else:
        raise ValueError(f"unknown trigger type: {agent.trigger}")


async def _trigger_cli(agent: AgentConfig, pr: PRMeta) -> str:
    """Run agent via shell command."""
    cmd = agent.command.format(
        pr_url=pr.pr_url,
        pr_id=pr.pr_id,
        project=pr.project,
        repo=pr.repo,
    )

    log.info("trigger cli: %s", cmd[:200])

    proc = await asyncio.create_subprocess_shell(
        cmd,
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


async def _trigger_http(agent: AgentConfig, pr: PRMeta, command: str) -> str:
    """Trigger agent via HTTP POST."""
    import urllib.request
    import json

    url = f"{agent.base_url.rstrip('/')}/{command}"
    payload = json.dumps({
        "pr_url": pr.pr_url,
        "pr_id": pr.pr_id,
        "project": pr.project,
        "repo": pr.repo,
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
