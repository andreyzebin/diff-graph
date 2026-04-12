"""
FastAPI webhook server — receives Bitbucket events, routes to agents.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response

from .config import load_config, WebhookConfig
from .bitbucket import parse_event, extract_commands
from .router import route_event, ForwardDecision, CommandDecision
from .triggers import trigger_agent

log = logging.getLogger(__name__)

app = FastAPI(title="DiffGraph Webhook Router")

_config: WebhookConfig | None = None


def init_app(config_path: str | Path) -> FastAPI:
    """Initialize the app with config."""
    global _config
    _config = load_config(config_path)
    log.info("loaded %d agents, %d routes", len(_config.agents), len(_config.routes))
    return app


@app.post("/webhook")
async def handle_webhook(request: Request):
    """Receive Bitbucket Server webhook event."""
    if _config is None:
        return Response(status_code=500, content="not configured")

    body = await request.body()

    # Signature verification
    if _config.secret:
        sig = request.headers.get("x-hub-signature", "")
        expected = "sha256=" + hmac.new(
            _config.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            log.warning("signature mismatch")
            return Response(status_code=401, content="invalid signature")

    import json
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=400, content="invalid json")

    # Test ping
    if data.get("test"):
        return {"status": "ok", "message": "webhook connected"}

    # Parse event
    server_url = request.headers.get("x-bitbucket-server-url", "")
    event = parse_event(data, server_url)
    if not event:
        return {"status": "ignored", "reason": "no PR in event"}

    log.info("event=%s project=%s repo=%s pr=#%s",
             event.event_key, event.pr.project, event.pr.repo, event.pr.pr_id)

    # Extract commands
    commands = extract_commands(event, _config.events)

    # Route
    result = route_event(commands, event.pr, _config)

    # Forward decision — event-level
    if isinstance(result, ForwardDecision):
        agent = _config.agents[result.agent_name]
        asyncio.create_task(_run_forward(agent, event, result, data))
        return {
            "status": "accepted",
            "mode": "forward",
            "agent": result.agent_name,
            "route": result.route_name,
        }

    # Command decisions — command-level
    if isinstance(result, list) and result:
        for d in result:
            agent = _config.agents[d.agent_name]
            asyncio.create_task(_run_command(agent, event, d, data))
        return {
            "status": "accepted",
            "mode": "commands",
            "decisions": [
                {"command": d.command.name, "args": d.command.args,
                 "agent": d.agent_name, "route": d.route_name}
                for d in result
            ],
        }

    return {"status": "ignored", "reason": "no route matched"}


async def _run_forward(agent, event, decision, raw_event):
    """Forward raw event to agent."""
    try:
        result = await trigger_agent(agent, event.pr, None, raw_event)
        log.info("PR #%s forward:%s → %s", event.pr.pr_id, decision.agent_name, result)
    except Exception as exc:
        log.error("PR #%s forward:%s failed: %s", event.pr.pr_id, decision.agent_name, exc)


async def _run_command(agent, event, decision, raw_event):
    """Run command on agent."""
    try:
        result = await trigger_agent(agent, event.pr, decision.command, raw_event)
        log.info("PR #%s %s:%s → %s", event.pr.pr_id,
                 decision.command.name, decision.agent_name, result)
    except Exception as exc:
        log.error("PR #%s %s:%s failed: %s", event.pr.pr_id,
                  decision.command.name, decision.agent_name, exc)


@app.get("/health")
async def health():
    """Health check."""
    if _config is None:
        return {"status": "not configured"}
    return {
        "status": "ok",
        "agents": list(_config.agents.keys()),
        "routes": len(_config.routes),
    }


@app.get("/routes")
async def show_routes():
    """Show configured routes (for debugging)."""
    if _config is None:
        return {"error": "not configured"}
    return {
        "routes": [
            {
                "name": r.name,
                "when": r.when,
                "forward": r.forward,
                "agent": r.agent,
                "sample": r.sample,
                **r.commands,
            }
            for r in _config.routes
        ]
    }
