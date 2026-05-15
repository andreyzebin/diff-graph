"""Pusher / guard message catalog.

Loads `orchestra/messages.yaml` once at module import. Callers look up
messages by dotted key via `msg("budget.token.nudge", default="…")`.
If the YAML is missing or unreadable the loader returns an empty dict
and every lookup falls back to the caller's `default` — the current
in-code defaults stay correct in that path, so removing the YAML
never crashes startup.

Placeholders use `str.format` syntax. Apply-time substitution is the
caller's responsibility (`msg(...).format(threshold=...)`).

Why a separate module: pusher classes define their default messages
as class attributes evaluated at import time. Having one place that
loads + exposes the catalog keeps that pattern intact (class-attr
lookups are cheap, no per-instance YAML parsing) while letting an
operator review every wording in one file.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_MESSAGES_FILE = Path(__file__).parent / "messages.yaml"


def _load() -> dict:
    if not _MESSAGES_FILE.is_file():
        return {}
    try:
        with open(_MESSAGES_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001 — defensive at startup
        log.warning("failed to load %s: %s — falling back to inline defaults",
                    _MESSAGES_FILE, exc)
        return {}


_DATA: dict = _load()


def msg(dotted_key: str, default: str = "") -> str:
    """Look up a message by dotted key (e.g. "budget.token.nudge").

    Returns the catalog value or `default` if any segment of the key
    is missing or the leaf isn't a string. Strips leading/trailing
    whitespace — YAML's `>-` folded scalars sometimes leave one.
    """
    cur: Any = _DATA
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    if not isinstance(cur, str):
        return default
    return cur.strip()
