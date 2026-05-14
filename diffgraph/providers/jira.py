"""Jira provider — fetch a ticket's context for the reviewer.

The reviewer's PR claims to fix a Jira ticket; reading that ticket
(its description / acceptance criteria, the discussion, the state
history, the linked tickets) turns a narrow diff review into one
grounded in the broader effort.

Two layers, deliberately split so the network half and the parsing
half can be tested independently:

  - `JiraProvider.fetch_ticket_raw(key)` — the network call. One
    `issue(key, expand=changelog)` request gets fields + comments +
    changelog + issue links in a single round-trip.
  - `distill_ticket(raw)` — a PURE function: raw Jira JSON →
    `TicketContext`. This is where context discipline lives (cap
    comment count, truncate bodies, reduce the changelog to status
    transitions only). Unit-tested against a sanitized fixture; no
    network needed.

  `JiraProvider.fetch_ticket(key)` is just the composition.

Auth: bearer PAT via `JIRA_TOKEN`. Base URL via `JIRA_URL`
(defaults to the sberworks host). TLS reuses the same client cert +
CA bundle as the Bitbucket provider — Jira sits on the same host.

Graceful degradation: with no `JIRA_TOKEN` the provider still
constructs, but `fetch_ticket` returns a `TicketContext` flagged
`configured=False` carrying a "Jira not configured" note instead of
raising — diff-graph stays runnable standalone.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Context-discipline caps — a ticket with 80 comments and a fat
# changelog would otherwise blow the agent's token budget (same
# concern the diff tools cap at 30k chars).
MAX_COMMENTS = 20            # keep the most recent N
MAX_BODY_CHARS = 2000        # per comment body / description
DEFAULT_JIRA_URL = "https://sberworks.ru/jira"


@dataclass
class TicketComment:
    author: str
    created: str
    body: str


@dataclass
class StatusChange:
    """One status transition pulled from the changelog. Field edits,
    assignee churn, etc. are dropped — only `status` transitions
    survive the distill."""
    at: str
    by: str
    from_status: str
    to_status: str


@dataclass
class TicketLink:
    """An issue link, flattened for the agent: relationship + the
    other ticket's key/summary/status. `direction` is inward/outward
    so the agent can tell 'blocks' from 'is blocked by'."""
    relationship: str
    direction: str               # "inward" | "outward"
    key: str
    summary: str
    status: str


@dataclass
class TicketContext:
    key: str
    summary: str
    issue_type: str
    status: str
    description: str
    comments: list[TicketComment] = field(default_factory=list)
    status_history: list[StatusChange] = field(default_factory=list)
    links: list[TicketLink] = field(default_factory=list)
    comments_truncated: int = 0   # how many comments were dropped by the cap
    configured: bool = True
    note: str = ""                # set when configured=False


def _truncate(text: Optional[str], limit: int = MAX_BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated; {len(text)} chars total]"


def distill_ticket(raw: dict) -> TicketContext:
    """Pure: raw Jira issue JSON (`issue(key, expand=changelog)`
    shape) → `TicketContext`. All context-discipline lives here.

    Tolerant of missing keys — Jira's payload varies by issue type,
    permissions, and server version; a missing `comment` or
    `changelog` block must degrade to an empty list, not a KeyError.
    """
    fields = raw.get("fields") or {}

    summary = (fields.get("summary") or "").strip()
    issue_type = ((fields.get("issuetype") or {}).get("name") or "").strip()
    status = ((fields.get("status") or {}).get("name") or "").strip()
    description = _truncate(fields.get("description"))

    # ── Comments — cap to the most recent MAX_COMMENTS ──────────────
    raw_comments = ((fields.get("comment") or {}).get("comments")) or []
    truncated = max(0, len(raw_comments) - MAX_COMMENTS)
    comments = [
        TicketComment(
            author=((c.get("author") or {}).get("displayName") or "?").strip(),
            created=(c.get("created") or "").strip(),
            body=_truncate(c.get("body")),
        )
        for c in raw_comments[-MAX_COMMENTS:]
    ]

    # ── Changelog — keep ONLY status transitions ────────────────────
    status_history: list[StatusChange] = []
    histories = ((raw.get("changelog") or {}).get("histories")) or []
    for h in histories:
        author = ((h.get("author") or {}).get("displayName") or "?").strip()
        at = (h.get("created") or "").strip()
        for item in (h.get("items") or []):
            if item.get("field") != "status":
                continue
            status_history.append(StatusChange(
                at=at,
                by=author,
                from_status=(item.get("fromString") or "").strip(),
                to_status=(item.get("toString") or "").strip(),
            ))

    # ── Issue links — flatten inward/outward into one list ──────────
    links: list[TicketLink] = []
    for l in (fields.get("issuelinks") or []):
        link_type = l.get("type") or {}
        if "outwardIssue" in l:
            other = l["outwardIssue"]
            rel, direction = (link_type.get("outward") or "relates to"), "outward"
        elif "inwardIssue" in l:
            other = l["inwardIssue"]
            rel, direction = (link_type.get("inward") or "relates to"), "inward"
        else:
            continue
        of = other.get("fields") or {}
        links.append(TicketLink(
            relationship=rel.strip(),
            direction=direction,
            key=(other.get("key") or "").strip(),
            summary=(of.get("summary") or "").strip(),
            status=((of.get("status") or {}).get("name") or "").strip(),
        ))

    return TicketContext(
        key=(raw.get("key") or "").strip(),
        summary=summary,
        issue_type=issue_type,
        status=status,
        description=description,
        comments=comments,
        status_history=status_history,
        links=links,
        comments_truncated=truncated,
    )


def _not_configured(key: str) -> TicketContext:
    return TicketContext(
        key=key, summary="", issue_type="", status="", description="",
        configured=False,
        note="Jira is not configured (JIRA_TOKEN unset) — proceeding "
             "without ticket context.",
    )


def _not_viewable(key: str, exc: Exception) -> TicketContext:
    """Jira IS configured, but THIS ticket couldn't be read — a 404
    (deleted / never existed) or a 403 (the bot account lacks
    permission), or any other per-request failure. This is a normal
    condition, not a crash: a sentinel `TicketContext` so the agent
    proceeds on the diff alone. `configured` stays True (Jira itself
    is fine — distinct from the no-token case)."""
    return TicketContext(
        key=key, summary="", issue_type="", status="", description="",
        configured=True,
        note=(
            f"Ticket {key} could not be read — it may have been deleted, "
            f"or this account lacks permission to view it "
            f"({type(exc).__name__}). Proceeding with the diff + PR "
            f"description alone."
        ),
    )


def format_ticket(tc: TicketContext) -> str:
    """Stable text render of a `TicketContext` — the `read_ticket`
    tool's return contract.

    Why a fixed format matters: the fake (fixture-fed) provider and
    the real one both flow through `distill_ticket` → `format_ticket`,
    so the agent sees the EXACT same shape whether the ticket came
    off the wire or out of a test fixture. A test that mocked the
    tool's *output string* directly would risk drifting from the
    real format — mocking at the provider level + one renderer keeps
    them in lockstep.
    """
    # Note-only contexts — Jira not configured, OR this ticket
    # wasn't viewable (deleted / no permission). Either way there's
    # nothing to render but the note; surface it as a clean one-liner
    # so the agent reads it and moves on.
    if not tc.configured or (tc.note and not tc.summary):
        return f"[ticket {tc.key}] {tc.note}"

    lines: list[str] = [
        f"TICKET {tc.key} — {tc.issue_type or '?'} — {tc.status or '?'}",
        f"Summary: {tc.summary}",
    ]
    if tc.description:
        lines += ["", "Description / acceptance criteria:", tc.description]

    if tc.comments:
        lines += ["", f"Comments ({len(tc.comments)}"
                  + (f", {tc.comments_truncated} older not shown" if tc.comments_truncated else "")
                  + "):"]
        for c in tc.comments:
            lines.append(f"  [{c.created} · {c.author}] {c.body}")

    if tc.status_history:
        lines += ["", "Status history:"]
        for s in tc.status_history:
            lines.append(
                f"  {s.from_status or '∅'} → {s.to_status or '∅'}  "
                f"({s.at} · {s.by})"
            )

    if tc.links:
        lines += ["", "Linked issues:"]
        for l in tc.links:
            arrow = "→" if l.direction == "outward" else "←"
            lines.append(
                f"  {l.relationship} {arrow} {l.key} [{l.status or '?'}] "
                f"{l.summary}"
            )

    return "\n".join(lines)


class JiraProvider:
    """Thin wrapper over `atlassian.Jira`. Lazily builds the client
    on first use so constructing the provider never does I/O and
    never fails for an unconfigured environment."""

    def __init__(
        self,
        url: str = "",
        token: str = "",
        ca_bundle: str = "",
        client_cert: str = "",
        fixture_path: str = "",
    ):
        self.url = url or os.environ.get("JIRA_URL", "") or DEFAULT_JIRA_URL
        self.token = token or os.environ.get("JIRA_TOKEN", "")
        self.ca_bundle = ca_bundle or os.environ.get("REQUESTS_CA_BUNDLE", "")
        self.client_cert = (
            client_cert
            or os.environ.get("BITBUCKET_SERVER_CLIENT_CERT", "")
            or os.environ.get("BITBUCKET_SERVER__CLIENT_CERT", "")
        )
        # Fake-provider switch — mirrors the fake-bitbucket pattern:
        # same class, env-switched data source. When set, the
        # network call is replaced by a read of this fixture JSON
        # (a single raw `issue(expand=changelog)` payload, returned
        # for any requested key). The bench's run_unit passes this
        # via DIFFGRAPH_JIRA_FIXTURE for ticket-backed scenarios.
        self.fixture_path = fixture_path or os.environ.get(
            "DIFFGRAPH_JIRA_FIXTURE", ""
        )
        self._client = None

    @property
    def configured(self) -> bool:
        # A fixture counts as "configured" — it IS the data source
        # for the fake path, no token needed.
        return bool(self.token) or bool(self.fixture_path)

    def _jira(self):
        if self._client is None:
            from atlassian import Jira
            self._client = Jira(
                url=self.url,
                token=self.token,
                # `verify_ssl` maps straight to requests' `verify`: a
                # CA-bundle path or True. `cert` is the mTLS client
                # cert — same PEM the Bitbucket provider uses.
                verify_ssl=self.ca_bundle or True,
                cert=self.client_cert or None,
            )
        return self._client

    def fetch_ticket_raw(self, key: str) -> dict:
        """Raw `issue(expand=changelog)` payload. Reads the fixture
        file when DIFFGRAPH_JIRA_FIXTURE is set (fake path), else
        hits the network. A missing fixture file fails loud — that's
        a test misconfiguration, not a runtime condition to paper
        over."""
        if self.fixture_path:
            import json
            from pathlib import Path
            p = Path(self.fixture_path).expanduser()
            if not p.is_file():
                raise FileNotFoundError(
                    f"DIFFGRAPH_JIRA_FIXTURE points at a missing file: {p}"
                )
            return json.loads(p.read_text(encoding="utf-8"))
        return self._jira().issue(key, expand="changelog")

    def fetch_ticket(self, key: str) -> TicketContext:
        """`fetch_ticket_raw` + `distill_ticket`, with graceful
        degradation at three levels:

          - no token / no fixture        → `_not_configured` sentinel
          - fixture mode, any error      → propagates (a broken test
                                           fixture must fail loud)
          - network mode, any error      → `_not_viewable` sentinel
                                           (404 deleted / 403 no
                                           permission / timeout / …)

        The network-mode catch is what keeps the reviewer stable when
        Jira says "you can't view this issue": a deleted or
        permission-locked ticket is a normal per-PR condition, not a
        crash — the agent gets a clean note and proceeds on the diff."""
        if not self.configured:
            return _not_configured(key)
        if self.fixture_path:
            # Fake path — a missing/malformed fixture is a test
            # misconfiguration; don't paper over it.
            return distill_ticket(self.fetch_ticket_raw(key))
        try:
            return distill_ticket(self.fetch_ticket_raw(key))
        except Exception as exc:
            log.info("read_ticket: %s not viewable (%s): %s",
                     key, type(exc).__name__, exc)
            return _not_viewable(key, exc)
