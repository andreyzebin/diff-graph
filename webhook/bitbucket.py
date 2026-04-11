"""
Parse Bitbucket Server webhook payloads into PR metadata + commands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PRMeta:
    project: str
    repo: str
    pr_id: int
    pr_url: str
    author: str
    branch: str
    target: str
    title: str


@dataclass
class WebhookEvent:
    event_key: str  # pr:opened, pr:comment:added, repo:refs_changed
    pr: PRMeta
    comment_text: str = ""  # only for pr:comment:added


def parse_event(data: dict, server_url: str = "") -> WebhookEvent | None:
    """Parse Bitbucket Server webhook JSON into WebhookEvent."""
    event_key = data.get("eventKey", "")
    pr_data = data.get("pullRequest")

    if not pr_data:
        return None

    pr_id = pr_data.get("id", -1)
    if pr_id == -1:
        return None

    from_ref = pr_data.get("fromRef", {})
    to_ref = pr_data.get("toRef", {})
    from_repo = from_ref.get("repository", {})
    project = from_repo.get("project", {}).get("key", "")
    repo = from_repo.get("slug", "")

    # Build PR URL
    if server_url:
        pr_url = f"{server_url}/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
    else:
        # Try to infer from links
        links = pr_data.get("links", {}).get("self", [{}])
        pr_url = links[0].get("href", "") if links else ""

    pr = PRMeta(
        project=project,
        repo=repo,
        pr_id=pr_id,
        pr_url=pr_url,
        author=pr_data.get("author", {}).get("user", {}).get("name", ""),
        branch=from_ref.get("displayId", ""),
        target=to_ref.get("displayId", ""),
        title=pr_data.get("title", ""),
    )

    comment_text = ""
    if event_key == "pr:comment:added":
        comment_text = data.get("comment", {}).get("text", "")

    return WebhookEvent(event_key=event_key, pr=pr, comment_text=comment_text)


_COMMAND_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$", re.DOTALL)


def extract_commands(event: WebhookEvent, events_config: dict) -> list[str]:
    """
    Determine which commands to run for this event.

    Returns list of command names like ["review", "describe"].
    """
    cfg = events_config.get(event.event_key)

    if cfg is None:
        return []

    # "parse" — extract /command from comment text
    if cfg == "parse":
        if not event.comment_text:
            return []
        m = _COMMAND_RE.match(event.comment_text.strip())
        if m:
            return [m.group(1)]
        return []

    # List of auto-commands
    if isinstance(cfg, list):
        return list(cfg)

    return []
