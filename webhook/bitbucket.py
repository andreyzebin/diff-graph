"""
Parse Bitbucket Server webhook payloads into PR metadata + commands.

Supports:
- Auto commands on pr:opened (from config)
- /command extraction from comments (with @mention filtering)
- Command arguments (/ask "question", /help "topic")
- Parent comment ID for threaded replies (/improve in a thread)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


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
class CommandRequest:
    """A parsed command with its arguments and context."""
    name: str                    # "review", "ask", "improve", etc.
    args: str = ""               # text after command: question, instructions
    comment_id: int | None = None  # ID of the comment that invoked the command


@dataclass
class WebhookEvent:
    event_key: str  # pr:opened, pr:comment:added, repo:refs_changed
    pr: PRMeta
    comment_text: str = ""
    comment_id: int | None = None      # ID of the comment itself
    parent_comment_id: int | None = None  # parent ID if reply in thread


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
    comment_id = None
    parent_comment_id = None

    if event_key == "pr:comment:added":
        comment_data = data.get("comment", {})
        comment_text = comment_data.get("text", "")
        comment_id = comment_data.get("id")
        # Bitbucket nests parent in comment.parent.id for thread replies
        parent = comment_data.get("parent")
        if parent:
            parent_comment_id = parent.get("id")

    return WebhookEvent(
        event_key=event_key, pr=pr,
        comment_text=comment_text,
        comment_id=comment_id,
        parent_comment_id=parent_comment_id,
    )


# Match: optional @mention, then /command, then optional args
# Examples:
#   /review
#   @diffgraph /review
#   @diffgraph /ask What about null safety?
#   /improve --focus=security
_COMMAND_RE = re.compile(
    r"(?:@\w+\s+)?/(\w+)(?:\s+(.*))?$",
    re.DOTALL,
)


def extract_commands(event: WebhookEvent, events_config: dict) -> list[CommandRequest]:
    """
    Determine which commands to run for this event.

    Returns list of CommandRequest with name, args, and context.
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
            name = m.group(1)
            args = (m.group(2) or "").strip()
            return [CommandRequest(
                name=name,
                args=args,
                comment_id=event.comment_id,
            )]
        return []

    # List of auto-commands
    if isinstance(cfg, list):
        return [CommandRequest(name=cmd) for cmd in cfg]

    return []
