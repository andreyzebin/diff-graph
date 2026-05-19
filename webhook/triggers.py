"""
Agent trigger mechanisms — execute agent for a PR.

Placeholders available in command templates:
  {pr_url}      — full PR URL
  {pr_id}       — PR number
  {project}     — Bitbucket project key
  {repo}        — repository slug
  {command}     — command name (review, help, default, ...)
  {args}        — command arguments (question text, instructions)
  {message}     — full user message ("/review", "/help topic", or raw text for default)
  {comment_id}  — parent comment ID (for threaded replies), empty if none
"""
from __future__ import annotations

import asyncio
import logging
import os

from .config import AgentConfig
from .bitbucket import PRMeta, CommandRequest

log = logging.getLogger(__name__)


def _get_ssl_context():
    """Create SSL context using OS trust store + optional CA/client certs."""
    import ssl
    from pathlib import Path
    ctx = ssl.create_default_context()
    ca = os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and Path(ca).exists():
        ctx.load_verify_locations(ca)
    client_cert = os.environ.get("BITBUCKET_SERVER_CLIENT_CERT") or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT")
    if client_cert and Path(client_cert).exists():
        ctx.load_cert_chain(client_cert)
    return ctx


async def trigger_agent(
    agent: AgentConfig, pr: PRMeta, cmd: CommandRequest,
    raw_event: dict | None = None,
) -> str:
    """
    Trigger an agent for a PR command. Returns output summary.
    """
    if agent.trigger == "cli":
        return await _trigger_cli(agent, pr, cmd)
    elif agent.trigger == "http":
        return await _trigger_http(agent, pr, cmd)
    elif agent.trigger == "webhook":
        return await _trigger_webhook(agent, raw_event)
    else:
        raise ValueError(f"unknown trigger type: {agent.trigger}")


def _build_placeholders(pr: PRMeta, cmd: CommandRequest) -> dict[str, str]:
    """Build placeholder dict for command template substitution."""
    # Build message: for /commands → "/command args", for default → raw text
    if cmd.name == "default":
        message = cmd.args or ""
    elif cmd.args:
        message = f"/{cmd.name} {cmd.args}"
    else:
        message = f"/{cmd.name}"

    return {
        "pr_url": pr.pr_url,
        "pr_id": str(pr.pr_id),
        "project": pr.project,
        "repo": pr.repo,
        "command": cmd.name,
        "args": cmd.args or "",
        "message": message,
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

    # Forward recording opt-in via env so cli.py can capture this run
    # without needing the per-route flag baked into the shell template.
    sub_env = None
    if agent.recording_dir:
        sub_env = {**os.environ}
        sub_env["DIFFGRAPH_RECORD_DIR"] = os.path.expanduser(agent.recording_dir)
        sub_env["DIFFGRAPH_RECORD_SCOPE"] = agent.recording_scope or "range"

    proc = await asyncio.create_subprocess_shell(
        command,
        executable="/bin/bash",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
        cwd=os.path.expanduser("~/"),
        env=sub_env,
    )

    # Stream output lines in real time
    prefix = f"[{agent.name}]"
    try:
        async def _stream():
            assert proc.stdout
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    log.info("%s %s", prefix, line)
            return await proc.wait()

        returncode = await asyncio.wait_for(_stream(), timeout=agent.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"agent {agent.name} timed out after {agent.timeout}s"
        log.error(msg)
        return msg

    if returncode != 0:
        msg = f"agent {agent.name} failed (exit {returncode})"
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
    ssl_ctx = _get_ssl_context()

    try:
        with urllib.request.urlopen(req, timeout=agent.timeout, context=ssl_ctx) as resp:
            return f"agent {agent.name} responded {resp.status}"
    except Exception as exc:
        msg = f"agent {agent.name} http error: {exc}"
        log.error(msg)
        return msg


async def _trigger_webhook(agent: AgentConfig, raw_event: dict | None) -> str:
    """Forward raw Bitbucket event to another webhook endpoint."""
    import urllib.request
    import json

    if not raw_event:
        return f"agent {agent.name}: no raw event to forward"

    url = agent.base_url
    payload = json.dumps(raw_event).encode()

    headers = {"Content-Type": "application/json"}
    if agent.api_key:
        headers["x-hub-signature"] = agent.api_key

    log.info("forward webhook [%s]: %s", agent.name, url)

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    ssl_ctx = _get_ssl_context()

    try:
        with urllib.request.urlopen(req, timeout=agent.timeout, context=ssl_ctx) as resp:
            return f"agent {agent.name} webhook forwarded ({resp.status})"
    except Exception as exc:
        msg = f"agent {agent.name} webhook forward error: {exc}"
        log.error(msg)
        return msg
