"""
TOML configuration loader for webhook router.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    name: str
    trigger: str  # "cli" or "http"
    command: str = ""  # default command template
    commands: dict[str, str] = field(default_factory=dict)  # per-command overrides
    base_url: str = ""
    api_key: str = ""
    timeout: int = 600


@dataclass
class Route:
    name: str
    when: str                     # Python expression
    agent: str | None = None      # command-level routing: agent for all commands
    forward: str | None = None    # event-level routing: forward raw event to this agent
    sample: int = 100             # percentage of PRs this route matches (by url hash)
    commands: dict[str, str] = field(default_factory=dict)  # per-command overrides


@dataclass
class WebhookConfig:
    agents: dict[str, AgentConfig]
    events: dict[str, list[str] | str]  # event_key → commands or "parse"
    routes: list[Route]
    secret: str = ""
    port: int = 8000


def load_config(path: str | Path) -> WebhookConfig:
    """Load webhook config from TOML file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    # Agents
    agents = {}
    for name, cfg in raw.get("agents", {}).items():
        per_cmd = {}
        for k, v in cfg.get("commands", {}).items():
            per_cmd[k] = str(v)
        agents[name] = AgentConfig(
            name=name,
            trigger=cfg.get("trigger", "cli"),
            command=cfg.get("command", ""),
            commands=per_cmd,
            base_url=cfg.get("base_url", ""),
            api_key=cfg.get("api_key", ""),
            timeout=cfg.get("timeout", 600),
        )

    # Events
    events = {}
    for key, val in raw.get("events", {}).items():
        if isinstance(val, str):
            events[key] = val  # "parse"
        else:
            events[key] = list(val)

    # Routes
    routes = []
    for r in raw.get("routes", []):
        commands = {}
        known_keys = {"name", "when", "agent", "forward", "sample"}
        for k, v in r.items():
            if k not in known_keys:
                commands[k] = v
        routes.append(Route(
            name=r.get("name", ""),
            when=r.get("when", "true"),
            agent=r.get("agent"),
            forward=r.get("forward"),
            sample=r.get("sample", 100),
            commands=commands,
        ))

    return WebhookConfig(
        agents=agents,
        events=events,
        routes=routes,
        secret=raw.get("server", {}).get("secret", ""),
        port=raw.get("server", {}).get("port", 8000),
    )
