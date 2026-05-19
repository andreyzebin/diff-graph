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
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

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
class SearchResult:
    """One row of a `search_tickets` response. Three fields per
    TODO §5b Phase 2 — `key + summary + status`. Agents copy `key`
    verbatim into `jira_read_ticket` to drill in."""
    key: str
    summary: str
    status: str
    issue_type: str = ""


@dataclass
class SearchResults:
    """Distilled output of `JiraProvider.search_tickets`. `total` is
    the server-side total when the response carries it (real Jira),
    or `len(results)` for fakes."""
    jql: str
    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    truncated: bool = False    # True when limit clipped the response
    configured: bool = True
    note: str = ""             # set when configured=False or on error


@dataclass
class DevInfoBranch:
    name: str
    url: str
    repository_name: str
    repository_url: str


@dataclass
class DevInfoCommit:
    id: str               # full SHA when available
    display_id: str       # short SHA
    url: str
    message: str
    author: str
    repository_name: str
    repository_url: str


@dataclass
class DevInfoPullRequest:
    id: str               # PR number as string
    name: str             # PR title
    url: str
    status: str           # OPEN / MERGED / DECLINED / SUPERSEDED
    source_branch: str
    destination_branch: str
    author: str
    repository_name: str
    repository_url: str
    # `repo_uri` is the bitbucket://handle/project/repo address —
    # computed from `repository_url` so the agent can pass it
    # straight to `pr_get(repo=..., pr=id)`. Empty string when the
    # URL doesn't parse (unknown host layout).
    repo_uri: str = ""


@dataclass
class DevInfoContext:
    """Distilled output of `JiraProvider.fetch_dev_info` — the
    branches / commits / pull-requests Jira has linked to a ticket.
    Empty lists are not an error — they mean "no dev activity
    linked yet" or the dev-status integration isn't configured."""
    key: str
    branches: list[DevInfoBranch] = field(default_factory=list)
    commits: list[DevInfoCommit] = field(default_factory=list)
    pull_requests: list[DevInfoPullRequest] = field(default_factory=list)
    configured: bool = True
    note: str = ""        # set when configured=False


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
        note="Jira is not configured (JIRA_TOKEN unset) — proceed.",
    )


def _disabled(key: str) -> TicketContext:
    """Jira deliberately switched OFF for this run — the operator
    toggle (DIFFGRAPH_JIRA_DISABLED), distinct from "never set up".
    The bench sets this for every unit scenario that doesn't opt in
    with a `jira_fixture:`, so `jira_read_ticket` being in the
    reviewer/investigator base toolset never makes a scenario reach
    for a live Jira. Same shape of outcome as the other sentinels:
    a one-line note that names the tool state and says "proceed" —
    nothing about WHAT to fall back on (that's the agent's call, not
    the tool's to dictate)."""
    return TicketContext(
        key=key, summary="", issue_type="", status="", description="",
        configured=False,
        note="Jira integration is disabled for this run — proceed.",
    )


def _not_viewable(key: str, exc: Exception) -> TicketContext:
    """Jira IS configured, but THIS ticket couldn't be read — a 404
    (deleted / never existed) or a 403 (the bot account lacks
    permission), or any other per-request failure. This is a normal
    condition, not a crash: a sentinel `TicketContext` so the agent
    keeps going. `configured` stays True (Jira itself is fine —
    distinct from the no-token case)."""
    return TicketContext(
        key=key, summary="", issue_type="", status="", description="",
        configured=True,
        note=(
            f"Ticket {key} could not be read — it may have been deleted, "
            f"or this account lacks permission to view it "
            f"({type(exc).__name__}). Proceed."
        ),
    )


def format_ticket(tc: TicketContext) -> str:
    """Stable text render of a `TicketContext` — the `jira_read_ticket`
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


# ── dev-info (branches / commits / PRs Jira links to a ticket) ─────


_BITBUCKET_URL_RE = re.compile(
    r"https?://[^/]+/projects/(?P<project>[^/]+)/repos/(?P<repo>[^/?#]+)"
)


def _repo_uri_from_bitbucket_url(url: str, *, handle: str = "default") -> str:
    """Convert a Bitbucket Server repository URL into our
    `bitbucket://<handle>/<project>/<repo>` URI. Returns empty when
    the URL doesn't match the expected layout (e.g. cloud-style URLs,
    SSH refs, unconfigured deployments) — the agent then has to fall
    back to the human URL alone."""
    if not url:
        return ""
    m = _BITBUCKET_URL_RE.search(url)
    if not m:
        return ""
    return f"bitbucket://{handle}/{m.group('project')}/{m.group('repo')}"


def distill_dev_info(key: str, raw: dict, *, handle: str = "default") -> DevInfoContext:
    """Pure: raw dev-status payload → `DevInfoContext`. Flattens
    Atlassian's nested response, extracts the few fields the agent
    actually needs to navigate (branch name, commit SHA, PR id +
    repo URI), drops the rest.

    Tolerant of missing keys — dev-status responses vary by
    Bitbucket version + Application Link configuration; absent
    branches / commits / pullRequests blocks degrade to empty
    lists.
    """
    branches_raw = raw.get("branches") or []
    commits_raw = raw.get("commits") or []
    prs_raw = raw.get("pullRequests") or []

    branches: list[DevInfoBranch] = []
    for b in branches_raw:
        repo = (b or {}).get("repository") or {}
        branches.append(DevInfoBranch(
            name=str(b.get("name") or "").strip(),
            url=str(b.get("url") or "").strip(),
            repository_name=str(repo.get("name") or "").strip(),
            repository_url=str(repo.get("url") or "").strip(),
        ))

    commits: list[DevInfoCommit] = []
    for c in commits_raw:
        repo = (c or {}).get("repository") or {}
        author = (c or {}).get("author") or {}
        commits.append(DevInfoCommit(
            id=str(c.get("id") or "").strip(),
            display_id=str(c.get("displayId") or "").strip(),
            url=str(c.get("url") or "").strip(),
            message=str(c.get("message") or "").strip(),
            author=str(author.get("name") or "").strip(),
            repository_name=str(repo.get("name") or "").strip(),
            repository_url=str(repo.get("url") or "").strip(),
        ))

    pull_requests: list[DevInfoPullRequest] = []
    for p in prs_raw:
        repo = (p or {}).get("repository") or {}
        author = (p or {}).get("author") or {}
        source = (p or {}).get("source") or {}
        dest = (p or {}).get("destination") or {}
        repo_url = str(repo.get("url") or "").strip()
        pull_requests.append(DevInfoPullRequest(
            id=str(p.get("id") or "").lstrip("#").strip(),
            name=str(p.get("name") or "").strip(),
            url=str(p.get("url") or "").strip(),
            status=str(p.get("status") or "").strip().upper(),
            source_branch=str(source.get("branch") or "").strip(),
            destination_branch=str(dest.get("branch") or "").strip(),
            author=str(author.get("name") or "").strip(),
            repository_name=str(repo.get("name") or "").strip(),
            repository_url=repo_url,
            repo_uri=_repo_uri_from_bitbucket_url(repo_url, handle=handle),
        ))

    return DevInfoContext(
        key=key,
        branches=branches,
        commits=commits,
        pull_requests=pull_requests,
    )


def format_dev_info(di: DevInfoContext) -> str:
    """Stable text render of a `DevInfoContext`. Output is the
    `jira_dev_info` tool's return contract — the agent reads this
    string verbatim. Always lists PRs first (the most useful edge for
    cross-source navigation) with the exact `pr_get(repo=..., pr=...)`
    call shape pre-formatted, so the agent can copy-paste it."""
    if not di.configured or (di.note and not di.pull_requests
                              and not di.commits and not di.branches):
        return f"[dev_info {di.key}] {di.note or 'Jira dev-status unavailable.'}"

    if not (di.branches or di.commits or di.pull_requests):
        return (
            f"[dev_info {di.key}] No linked branches, commits, or PRs. "
            f"(Either none have been created yet, or the Application "
            f"Link between Jira and Bitbucket isn't wired.)"
        )

    lines: list[str] = [f"DEV_INFO {di.key}"]

    if di.pull_requests:
        lines += ["", f"Pull requests ({len(di.pull_requests)}):"]
        for p in di.pull_requests:
            head = (
                f"  #{p.id} [{p.status}] {p.name} "
                f"({p.source_branch} → {p.destination_branch}, "
                f"by {p.author or '?'} in {p.repository_name or '?'})"
            )
            lines.append(head)
            if p.repo_uri:
                lines.append(
                    f"      → pr_get(repo=\"{p.repo_uri}\", pr=\"{p.id}\")"
                )
            elif p.url:
                lines.append(f"      → URL: {p.url}")

    if di.branches:
        lines += ["", f"Branches ({len(di.branches)}):"]
        for b in di.branches:
            lines.append(
                f"  {b.name}  ({b.repository_name or '?'})"
            )

    if di.commits:
        lines += ["", f"Commits ({len(di.commits)}):"]
        for c in di.commits:
            short = c.display_id or c.id[:7]
            first_line = (c.message or "").splitlines()[0] if c.message else ""
            lines.append(
                f"  {short}  {first_line[:80]}  "
                f"(by {c.author or '?'} in {c.repository_name or '?'})"
            )

    return "\n".join(lines)


def _dev_info_not_configured(key: str) -> DevInfoContext:
    return DevInfoContext(
        key=key, configured=False,
        note="Jira dev-status is not configured for this deployment.",
    )


def _dev_info_disabled(key: str) -> DevInfoContext:
    return DevInfoContext(
        key=key, configured=False,
        note="Jira integration is disabled in this run.",
    )


# ── search_tickets (JQL discovery) ─────────────────────────────────


def distill_search_results(
    jql: str, raw: dict, *, limit: int = 0,
) -> SearchResults:
    """Pure: raw `Jira.jql(jql)` response → `SearchResults`. Tolerant
    of the variations Jira returns — some endpoints set `total`, some
    don't; some fields land under `fields.summary`, some at top level
    (depending on the search API version and the `fields=` param)."""
    issues_raw = raw.get("issues") or []
    if limit and len(issues_raw) > limit:
        issues_used = issues_raw[:limit]
        truncated = True
    else:
        issues_used = issues_raw
        truncated = False
    results: list[SearchResult] = []
    for issue in issues_used:
        fields = (issue or {}).get("fields") or {}
        summary = str(
            (fields.get("summary") if fields.get("summary") is not None
             else issue.get("summary")) or ""
        ).strip()
        status_obj = fields.get("status") or {}
        status = str(
            (status_obj.get("name") if isinstance(status_obj, dict)
             else status_obj) or issue.get("status") or ""
        ).strip()
        issue_type_obj = fields.get("issuetype") or {}
        issue_type = str(
            (issue_type_obj.get("name") if isinstance(issue_type_obj, dict)
             else issue_type_obj) or ""
        ).strip()
        results.append(SearchResult(
            key=str(issue.get("key") or "").strip(),
            summary=summary,
            status=status,
            issue_type=issue_type,
        ))
    total = int(raw.get("total") or len(issues_raw))
    return SearchResults(
        jql=jql,
        results=results,
        total=total,
        truncated=truncated,
    )


def format_search_results(sr: SearchResults) -> str:
    """Stable text render — the `search_tickets` tool's contract.
    One line per result, copy-paste friendly: the `key` is bare and
    the agent feeds it straight into `jira_read_ticket`."""
    if not sr.configured:
        return f"[search_tickets jql={sr.jql!r}] {sr.note or 'Jira unavailable.'}"

    if not sr.results:
        return (
            f"[search_tickets jql={sr.jql!r}] No matching tickets "
            f"(total={sr.total})."
        )

    lines: list[str] = [
        f"SEARCH_TICKETS jql={sr.jql!r}  "
        f"results={len(sr.results)}/{sr.total}"
        + ("  (truncated)" if sr.truncated else "")
    ]
    for r in sr.results:
        type_part = f"[{r.issue_type}] " if r.issue_type else ""
        lines.append(
            f"  {r.key}  {type_part}[{r.status or '?'}]  {r.summary}"
        )
    lines.append(
        "Drill in with: jira_read_ticket(\"<key>\") or "
        "jira_dev_info(\"<key>\") for linked PRs/branches/commits."
    )
    return "\n".join(lines)


def _search_results_not_configured(jql: str) -> SearchResults:
    return SearchResults(
        jql=jql, configured=False,
        note="Jira search is not configured for this deployment.",
    )


def _search_results_disabled(jql: str) -> SearchResults:
    return SearchResults(
        jql=jql, configured=False,
        note="Jira integration is disabled in this run.",
    )


# Wrapper keys that mark a fixture as the extended multi-fetch
# envelope (`{issue, dev_info, searches}`) rather than the legacy
# "the JSON IS the issue payload" shape. Presence of ANY of these
# at the top level flips the parser into envelope mode — so an
# extended fixture can ship just `{issue: ...}` or just
# `{searches: ...}` without having to populate every block.
_EXTENDED_FIXTURE_KEYS = frozenset({"issue", "dev_info", "searches"})


def _is_extended_fixture(blob: dict) -> bool:
    return any(k in blob for k in _EXTENDED_FIXTURE_KEYS)


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
        disabled: bool = False,
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
        # Operator toggle — DIFFGRAPH_JIRA_DISABLED forces every
        # jira_read_ticket to the `_disabled` sentinel regardless of token
        # / fixture. The bench sets it for unit scenarios that don't
        # opt into Jira; it also stands alone as a "Jira off" switch.
        self.disabled = disabled or bool(
            os.environ.get("DIFFGRAPH_JIRA_DISABLED", "")
        )
        self._client = None

    @property
    def configured(self) -> bool:
        # Disabled wins — an off provider is not "configured" no
        # matter what token/fixture is around. Otherwise a fixture
        # counts as configured (it IS the fake-path data source, no
        # token needed).
        if self.disabled:
            return False
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
        over.

        Fixture format supports two shapes for backward compatibility:
          - Legacy: the JSON file IS the issue payload — used as
            ticket body, dev_info is empty.
          - Extended: `{"issue": {...}, "dev_info": {...}}` — the
            `issue` key holds the ticket body, `dev_info` holds the
            shape `fetch_dev_info_raw` returns.
        """
        if self.fixture_path:
            blob = self._load_fixture()
            # Extended shape — any of the wrapper keys present →
            # treat the blob as the multi-fetch envelope and pull
            # the issue body out. Legacy shape (the blob IS the
            # issue payload) keeps working when none of those
            # wrapper keys appear.
            if isinstance(blob, dict) and _is_extended_fixture(blob):
                return blob.get("issue") or {}
            return blob
        return self._jira().issue(key, expand="changelog")

    def fetch_dev_info_raw(self, key: str) -> dict:
        """Raw dev-status payload — branches / commits / pull-requests
        Jira links to the issue. Real-network path hits Atlassian's
        `dev-status` API; fake-path reads from the fixture's
        `dev_info:` block (empty when fixture is legacy-shaped).

        Atlassian's dev-status endpoint requires the NUMERIC internal
        issue id, not the human key — so we fetch the issue first to
        resolve it. (For fixture mode the id lookup is skipped — the
        fixture already carries pre-baked dev_info.)
        """
        if self.fixture_path:
            blob = self._load_fixture()
            if isinstance(blob, dict) and _is_extended_fixture(blob):
                return blob.get("dev_info") or {}
            # Legacy fixture: no dev-info baked in.
            return {}
        # Real path — resolve numeric id, then call dev-status. Wrapped
        # in a try because dev-status isn't enabled on every Atlassian
        # install; an unsupported endpoint should degrade to empty
        # rather than crash the agent.
        issue = self._jira().issue(key, fields="id")
        issue_id = (issue or {}).get("id") or ""
        if not issue_id:
            return {}
        # Sum branches + commits + pull_requests for the bitbucket
        # application type. atlassian-python-api doesn't expose
        # dev-status directly, so use the raw GET.
        out: dict = {}
        for data_type in ("branch", "commit", "pullrequest"):
            try:
                resp = self._jira().get(
                    "rest/dev-status/1.0/issue/detail",
                    params={
                        "issueId": issue_id,
                        "applicationType": "bitbucket",
                        "dataType": data_type,
                    },
                )
            except Exception as exc:
                log.info("jira_dev_info: %s dataType=%s unavailable (%s): %s",
                         key, data_type, type(exc).__name__, exc)
                continue
            if isinstance(resp, dict):
                # Response shape: { detail: [{ branches: [...], commits: [...],
                # pullRequests: [...] }] }. Flatten into the merged dict.
                for entry in (resp.get("detail") or []):
                    for k, v in (entry or {}).items():
                        if v:
                            out.setdefault(k, []).extend(v)
        return out

    def _load_fixture(self) -> dict:
        """Read + parse the configured fixture JSON. Shared by
        `fetch_ticket_raw` and `fetch_dev_info_raw`."""
        import json
        from pathlib import Path
        p = Path(self.fixture_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(
                f"DIFFGRAPH_JIRA_FIXTURE points at a missing file: {p}"
            )
        return json.loads(p.read_text(encoding="utf-8"))

    def fetch_ticket(self, key: str) -> TicketContext:
        """`fetch_ticket_raw` + `distill_ticket`, with graceful
        degradation at four levels:

          - disabled (operator toggle)    → `_disabled` sentinel
          - no token / no fixture         → `_not_configured` sentinel
          - fixture mode, any error       → propagates (a broken test
                                            fixture must fail loud)
          - network mode, any error       → `_not_viewable` sentinel
                                            (404 deleted / 403 no
                                            permission / timeout / …)

        The network-mode catch is what keeps the reviewer stable when
        Jira says "you can't view this issue": a deleted or
        permission-locked ticket is a normal per-PR condition, not a
        crash — the agent gets a clean note and proceeds on the diff."""
        if self.disabled:
            return _disabled(key)
        if not self.configured:
            return _not_configured(key)
        if self.fixture_path:
            # Fake path — a missing/malformed fixture is a test
            # misconfiguration; don't paper over it.
            return distill_ticket(self.fetch_ticket_raw(key))
        try:
            return distill_ticket(self.fetch_ticket_raw(key))
        except Exception as exc:
            log.info("jira_read_ticket: %s not viewable (%s): %s",
                     key, type(exc).__name__, exc)
            return _not_viewable(key, exc)

    def search_tickets_raw(self, jql: str, *, limit: int = 20) -> dict:
        """Raw `Jira.jql(jql)` response (or fixture replay).

        Fake mode: the fixture's `searches:` block maps `<jql_pattern>`
        → `<raw_jql_response>` (the same shape `atlassian.Jira.jql`
        returns). Exact-string match on jql; a `"*"` entry serves as
        catch-all when no exact match. No entries → empty result set.
        """
        if self.fixture_path:
            blob = self._load_fixture()
            # Only the extended-shape fixture carries `searches:`.
            # Legacy issue-only fixtures have no search data → empty.
            if not (isinstance(blob, dict) and _is_extended_fixture(blob)):
                return {"issues": [], "total": 0}
            searches = blob.get("searches") or {}
            if not isinstance(searches, dict):
                return {"issues": [], "total": 0}
            # Try exact jql, then the catch-all `*`.
            for key in (jql, "*"):
                entry = searches.get(key)
                if isinstance(entry, dict):
                    return entry
                if isinstance(entry, list):
                    # Convenience: bare list of issue dicts → wrap.
                    return {"issues": entry, "total": len(entry)}
            return {"issues": [], "total": 0}
        return self._jira().jql(
            jql, limit=limit,
            fields="summary,status,issuetype",
        ) or {}

    def search_tickets(
        self, jql: str, *, limit: int = 20, handle: str = "default",
    ) -> SearchResults:
        """`search_tickets_raw` + `distill_search_results` with the
        same four-level graceful degradation pattern as the other
        fetch_* methods. Empty results are not an error — they mean
        the JQL just didn't match anything.
        """
        if self.disabled:
            return _search_results_disabled(jql)
        if not self.configured:
            return _search_results_not_configured(jql)
        if self.fixture_path:
            return distill_search_results(
                jql, self.search_tickets_raw(jql, limit=limit), limit=limit,
            )
        try:
            return distill_search_results(
                jql, self.search_tickets_raw(jql, limit=limit), limit=limit,
            )
        except Exception as exc:
            log.info("search_tickets: jql=%r unavailable (%s): %s",
                     jql, type(exc).__name__, exc)
            return SearchResults(
                jql=jql, configured=False,
                note=f"search endpoint unavailable: {type(exc).__name__}",
            )

    def fetch_dev_info(self, key: str, *, handle: str = "default") -> DevInfoContext:
        """`fetch_dev_info_raw` + `distill_dev_info`, with the same
        graceful-degradation pattern as `fetch_ticket`:

          - disabled                       → `_dev_info_disabled` sentinel
          - no token / no fixture          → `_dev_info_not_configured`
          - any error in fixture mode      → propagates (broken fixture)
          - any error in network mode      → empty `DevInfoContext`
                                             with a note (the endpoint
                                             isn't enabled on every
                                             Atlassian install)
        """
        if self.disabled:
            return _dev_info_disabled(key)
        if not self.configured:
            return _dev_info_not_configured(key)
        if self.fixture_path:
            return distill_dev_info(key, self.fetch_dev_info_raw(key), handle=handle)
        try:
            return distill_dev_info(key, self.fetch_dev_info_raw(key), handle=handle)
        except Exception as exc:
            log.info("jira_dev_info: %s unavailable (%s): %s",
                     key, type(exc).__name__, exc)
            return DevInfoContext(
                key=key, configured=False,
                note=f"dev-status endpoint unavailable: {type(exc).__name__}",
            )


# ── Multi-server registry + reference parsing ──────────────────────

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(value: str) -> str:
    """`${VAR}` → os.environ value (empty if unset). Secrets stay in
    the shell env; config.local.yaml only holds the ${VAR} reference."""
    if not isinstance(value, str):
        return value
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


@dataclass
class JiraServer:
    """One configured tracker instance. `handle` is the slash-free
    logical id used in ticket refs; the URL + auth are attributes,
    not the handle itself."""
    handle: str
    url: str
    token: str = ""
    ca_bundle: str = ""
    client_cert: str = ""


def parse_ticket_ref(ref: str) -> tuple[str, str, str]:
    """`handle/namespace/ticket_id` → (handle, namespace, ticket_id).

    Lenient — the agent copies refs verbatim from the PR's
    `jira_tickets` list, but a hand-typed bare key should still work:
      - 3+ parts  → (parts[0], parts[1], parts[-1])
      - 2 parts   → (parts[0], "", parts[1])
      - 1 part    → ("default", "", part)        # bare key
      - empty     → ("default", "", "")
    `namespace` is informational for Jira (the project is already in
    the key); it's the generality hook for non-Jira trackers."""
    parts = [p for p in (ref or "").strip().split("/") if p]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "default", "", (parts[0] if parts else "")


class JiraRegistry:
    """Resolves a server handle → `JiraProvider`.

    Multi-server: `config.local.yaml` `jira_servers:` maps handles to
    `{url, token, ...}`. The `default` handle (and any handle not in
    the config) falls back to a bare `JiraProvider()`, which reads
    `JIRA_URL` / `JIRA_TOKEN` from the env — so a single-server setup
    needs no config at all.

    The test-env overrides (`DIFFGRAPH_JIRA_DISABLED` /
    `DIFFGRAPH_JIRA_FIXTURE`) need no special handling here:
    `JiraProvider.__init__` reads them regardless of how it's
    constructed, so a disabled / fixture run answers every handle the
    same way."""

    def __init__(self, servers: Optional[dict] = None):
        self._servers: dict = servers or {}      # handle -> JiraServer
        self._cache: dict = {}                   # handle -> JiraProvider
        # Optional hook fired after every raw fetch — used by the
        # recording layer (TODO §19) to mirror each response to disk.
        # Signature: (kind: "ticket"|"dev_info"|"search", key_or_jql, raw_dict)
        # Set None to disable; tolerant of exceptions (capture is best-effort).
        self._record_hook: Optional[Callable[[str, str, dict], None]] = None

    def set_record_hook(
        self, hook: Optional[Callable[[str, str, dict], None]],
    ) -> None:
        self._record_hook = hook

    def _fire_hook(self, kind: str, key: str, raw: dict) -> None:
        if self._record_hook is None:
            return
        try:
            self._record_hook(kind, key, raw)
        except Exception as exc:
            log.warning("jira: record_hook(%s, %s) failed: %s", kind, key, exc)

    @classmethod
    def from_config(cls, config_path: Optional[str] = None) -> "JiraRegistry":
        """Load `jira_servers:` from config.local.yaml (repo root by
        default). Missing file / missing block → an empty registry,
        which still serves the env-based `default` provider."""
        from pathlib import Path
        p = Path(config_path) if config_path else (
            Path(__file__).resolve().parents[2] / "config.local.yaml"
        )
        servers: dict = {}
        if p.is_file():
            try:
                import yaml
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                for handle, sd in (raw.get("jira_servers") or {}).items():
                    sd = sd or {}
                    servers[str(handle)] = JiraServer(
                        handle=str(handle),
                        url=_expand_env(sd.get("url", "")),
                        token=_expand_env(sd.get("token", "")),
                        ca_bundle=_expand_env(sd.get("ca_bundle", "")),
                        client_cert=_expand_env(sd.get("client_cert", "")),
                    )
            except Exception as exc:
                log.warning("jira_servers config load failed (%s) — "
                            "falling back to env-based default", exc)
        return cls(servers)

    def provider_for(self, handle: str) -> JiraProvider:
        handle = handle or "default"
        if handle not in self._cache:
            srv = self._servers.get(handle)
            if srv:
                self._cache[handle] = JiraProvider(
                    url=srv.url, token=srv.token,
                    ca_bundle=srv.ca_bundle, client_cert=srv.client_cert,
                )
            else:
                # `default`, or an unknown handle — a bare provider
                # that reads JIRA_URL / JIRA_TOKEN (and the
                # DIFFGRAPH_JIRA_* test overrides) from the env.
                self._cache[handle] = JiraProvider()
        return self._cache[handle]

    def fetch(self, ref: str) -> TicketContext:
        """Parse a `handle/namespace/ticket_id` ref and fetch it from
        the right server. The one call the `jira_read_ticket` tool needs."""
        handle, _namespace, ticket_id = parse_ticket_ref(ref)
        prov = self.provider_for(handle)
        # Capture raw response for the recording before distillation —
        # gracefully tolerates _disabled/_not_configured sentinels which
        # don't call out to fetch_*_raw at all (hook just doesn't fire).
        if self._record_hook is not None and not prov.disabled and prov.configured:
            try:
                raw = prov.fetch_ticket_raw(ticket_id)
                self._fire_hook("ticket", ticket_id, raw)
                return distill_ticket(raw)
            except Exception:
                # fall through to the provider's own graceful handling
                pass
        return prov.fetch_ticket(ticket_id)

    def fetch_dev_info(self, ref: str) -> DevInfoContext:
        """Parse a `handle/namespace/ticket_id` ref and fetch its
        linked dev-status info (branches / commits / PRs). The one
        call the `jira_dev_info` tool needs. The PR-refs returned
        carry `repo_uri` set to `bitbucket://<handle>/...` using
        the ticket's own handle — letting the agent feed them
        straight back to `pr_get`."""
        handle, _namespace, ticket_id = parse_ticket_ref(ref)
        prov = self.provider_for(handle)
        if self._record_hook is not None and not prov.disabled and prov.configured:
            try:
                raw = prov.fetch_dev_info_raw(ticket_id)
                self._fire_hook("dev_info", ticket_id, raw)
                return distill_dev_info(raw, handle=handle)
            except Exception:
                pass
        return prov.fetch_dev_info(ticket_id, handle=handle)

    def search_tickets(
        self, jql: str, *, handle: str = "default", limit: int = 20,
    ) -> SearchResults:
        """Run a JQL query against the named server. Unlike
        `fetch` / `fetch_dev_info`, the search isn't tied to a
        specific ticket ref — so the handle is passed explicitly
        (defaulting to `"default"`). Multi-server deployments that
        want to search a non-default tracker pass e.g.
        `handle="internal"`."""
        prov = self.provider_for(handle)
        if self._record_hook is not None and not prov.disabled and prov.configured:
            try:
                raw = prov.search_tickets_raw(jql, limit=limit)
                self._fire_hook("search", jql, raw)
                return distill_search_results(jql, raw, limit=limit)
            except Exception:
                pass
        return prov.search_tickets(
            jql, limit=limit, handle=handle,
        )
