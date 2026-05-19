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
    # Recording (TODO §19) — when set, the spawned cli.py captures
    # the full PR state + diff + agent output under
    # <recording_dir>/<host>/<project>/<repo>/PR-<id>/. Forwarded as
    # DIFFGRAPH_RECORDINGS_DIR / DIFFGRAPH_RECORDINGS_SCOPE on the subprocess
    # env. Empty string disables capture for this agent.
    recording_dir: str = ""
    recording_scope: str = "range"  # "range" or "full" — see recording.py


@dataclass
class Route:
    name: str
    when: str                     # Python expression
    agent: str | None = None      # command-level routing: agent for all commands
    forward: str | None = None    # event-level routing: forward raw event to this agent
    sample: int = 100             # percentage of PRs this route matches (by url hash)
    commands: dict[str, str] = field(default_factory=dict)  # per-command overrides


@dataclass
class HealthCheck:
    """A single scheduled health-check entry."""
    name: str
    command: str                        # shell command to run; non-zero exit = unhealthy
    interval_seconds: int = 180         # how often to fire (default 3 min)
    timeout_seconds: int = 1200         # per-call timeout (default 20 min — covers cold start)
    time_window: str = ""               # "HH:MM-HH:MM" in `timezone`; empty = always
    timezone: str = "Europe/Moscow"     # IANA tz name
    days: list[int] = field(default_factory=list)  # ISO weekdays 1=Mon..7=Sun; empty = all


@dataclass
class WebhookConfig:
    agents: dict[str, AgentConfig]
    events: dict[str, list[str] | str]  # event_key → commands or "parse"
    routes: list[Route]
    health_checks: list[HealthCheck] = field(default_factory=list)
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
        rec_block = cfg.get("recording") or {}
        agents[name] = AgentConfig(
            name=name,
            trigger=cfg.get("trigger", "cli"),
            command=cfg.get("command", ""),
            commands=per_cmd,
            base_url=cfg.get("base_url", ""),
            api_key=cfg.get("api_key", ""),
            timeout=cfg.get("timeout", 600),
            recording_dir=str(rec_block.get("dir", "") or ""),
            recording_scope=str(rec_block.get("scope", "range") or "range"),
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

    # Health checks — keep LLM endpoints warm during working hours.
    raw_health = raw.get("health", [])
    if isinstance(raw_health, dict):
        # Support both [[health]] (list of tables) and one [health] block.
        raw_health = [raw_health]
    health_checks: list[HealthCheck] = []
    for idx, hc in enumerate(raw_health):
        health_checks.append(HealthCheck(
            name=hc.get("name", f"health-{idx}"),
            command=hc.get("command", ""),
            interval_seconds=int(hc.get("interval_seconds", 180)),
            timeout_seconds=int(hc.get("timeout_seconds", 1200)),
            time_window=hc.get("time_window", ""),
            timezone=hc.get("timezone", "Europe/Moscow"),
            days=list(hc.get("days", [])) or [],
        ))

    return WebhookConfig(
        agents=agents,
        events=events,
        routes=routes,
        health_checks=health_checks,
        secret=raw.get("server", {}).get("secret", ""),
        port=raw.get("server", {}).get("port", 8000),
    )
