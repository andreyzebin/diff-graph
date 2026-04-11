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
from .router import route_commands
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
    if not commands:
        return {"status": "ignored", "reason": "no commands for event"}

    # Route commands to agents
    decisions = route_commands(commands, event.pr, _config)
    if not decisions:
        return {"status": "ignored", "reason": "no routes matched"}

    # Trigger agents (in background — don't block webhook response)
    for d in decisions:
        agent = _config.agents[d.agent_name]
        asyncio.create_task(_run_agent(agent, event, d))

    return {
        "status": "accepted",
        "decisions": [
            {"command": d.command.name, "args": d.command.args,
             "agent": d.agent_name, "route": d.route_name}
            for d in decisions
        ],
    }


async def _run_agent(agent, event, decision):
    """Run agent in background, log result."""
    try:
        result = await trigger_agent(agent, event.pr, decision.command)
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
                "agent": r.agent,
                **r.commands,
            }
            for r in _config.routes
        ]
    }
