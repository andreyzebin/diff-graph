"""`diffgraph.providers.jira` — distill + graceful-degradation contract.

The provider is split so the two halves test independently:
  - `fetch_ticket_raw` is the network call — NOT unit-tested here
    (needs a live Jira).
  - `distill_ticket` is a pure function (raw Jira JSON →
    `TicketContext`) — that's what these tests pin, against a
    sanitized fixture derived from real sberworks SBLOOM tickets
    (tests/fixtures/jira_issue_sample.json) plus synthetic inputs
    for the edge cases the one real ticket couldn't cover.

What the fixture exercises in one pass: scalar fields, three
comments, a four-entry changelog of which only two are `status`
transitions (the Rank + assignee entries must be filtered out),
and two issue links — one outward ("is part of"), one inward
("is blocked by").
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from diffgraph.providers.jira import (
    JiraProvider,
    TicketContext,
    distill_ticket,
    format_ticket,
    _not_viewable,
    MAX_COMMENTS,
    MAX_BODY_CHARS,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "jira_issue_sample.json"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DISABLE_JIRA_FIXTURE = (
    _REPO_ROOT / "orchestra" / "fixtures" / "mocks" / "disable-jira.yaml"
)


@pytest.fixture
def raw_issue() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


# ── distill_ticket against the sanitized real-shape fixture ─────────

class TestDistillRealFixture:

    def test_scalar_fields(self, raw_issue):
        tc = distill_ticket(raw_issue)
        assert tc.key == "DEMO-101"
        assert tc.summary == "Add a small web console for the internal proxy"
        assert tc.issue_type == "Task"
        assert tc.status == "Resolved"
        assert tc.description.startswith("We keep hitting the proxy")
        assert tc.configured is True

    def test_comments_parsed_in_order(self, raw_issue):
        tc = distill_ticket(raw_issue)
        assert len(tc.comments) == 3
        assert tc.comments_truncated == 0
        assert [c.author for c in tc.comments] == [
            "Alex Developer", "Bella Reviewer", "Alex Developer",
        ]
        assert tc.comments[0].created == "2026-03-20T10:15:00.000+0300"
        assert "feature branch" in tc.comments[0].body

    def test_changelog_keeps_only_status_transitions(self, raw_issue):
        """The fixture's changelog has 4 histories — Rank, assignee,
        status, and a resolution+status pair. distill must drop the
        Rank and assignee ones and surface exactly the 2 status
        transitions, in order."""
        tc = distill_ticket(raw_issue)
        assert len(tc.status_history) == 2
        first, second = tc.status_history
        assert (first.from_status, first.to_status) == ("Open", "In Progress")
        assert first.by == "Alex Developer"
        assert (second.from_status, second.to_status) == (
            "In Progress", "Resolved",
        )
        assert second.by == "Bella Reviewer"

    def test_links_flattened_both_directions(self, raw_issue):
        """One outward link (PartOf → epic) and one inward link
        (Blocks ← a blocker task). distill flattens both into a
        single list, tagging direction so the agent can tell
        'blocks' from 'is blocked by'."""
        tc = distill_ticket(raw_issue)
        assert len(tc.links) == 2
        by_key = {l.key: l for l in tc.links}

        epic = by_key["DEMO-200"]
        assert epic.direction == "outward"
        assert epic.relationship == "is part of"
        assert epic.status == "In Progress"
        assert epic.summary == "Internal proxy — Q2 hardening epic"

        blocker = by_key["DEMO-099"]
        assert blocker.direction == "inward"
        assert blocker.relationship == "is blocked by"
        assert blocker.status == "Open"


# ── Edge cases the single real ticket couldn't cover ────────────────

class TestDistillEdgeCases:

    def test_comment_cap_keeps_most_recent_and_counts_dropped(self):
        """A ticket with more than MAX_COMMENTS comments: distill
        keeps the most recent MAX_COMMENTS and records how many it
        dropped, so the agent knows the history is partial."""
        n = MAX_COMMENTS + 5
        raw = {
            "key": "DEMO-1",
            "fields": {
                "comment": {"comments": [
                    {"author": {"displayName": f"User {i}"},
                     "created": f"2026-01-{(i % 28) + 1:02d}T00:00:00.000+0000",
                     "body": f"comment number {i}"}
                    for i in range(n)
                ]},
            },
        }
        tc = distill_ticket(raw)
        assert len(tc.comments) == MAX_COMMENTS
        assert tc.comments_truncated == 5
        # Kept the TAIL (most recent), not the head.
        assert tc.comments[-1].body == f"comment number {n - 1}"
        assert tc.comments[0].body == f"comment number {5}"

    def test_long_bodies_truncated(self):
        """Description and comment bodies past MAX_BODY_CHARS get a
        truncation marker — one unbounded ticket would otherwise
        blow the agent's token budget."""
        huge = "x" * (MAX_BODY_CHARS + 500)
        raw = {
            "key": "DEMO-2",
            "fields": {
                "description": huge,
                "comment": {"comments": [
                    {"author": {"displayName": "U"}, "created": "t",
                     "body": huge},
                ]},
            },
        }
        tc = distill_ticket(raw)
        assert len(tc.description) < len(huge)
        assert "truncated" in tc.description
        assert "truncated" in tc.comments[0].body

    def test_missing_blocks_degrade_to_empty(self):
        """Jira's payload varies by issue type / permissions /
        server version — a ticket with no comment / changelog /
        issuelinks blocks must distill to empty lists, never a
        KeyError. (This is the SBLOOM-144 shape — the real ticket
        that kicked off the spike was exactly this sparse.)"""
        raw = {
            "key": "DEMO-3",
            "fields": {
                "summary": "bare ticket",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "description": "nothing else here",
            },
        }
        tc = distill_ticket(raw)
        assert tc.key == "DEMO-3"
        assert tc.summary == "bare ticket"
        assert tc.comments == []
        assert tc.status_history == []
        assert tc.links == []
        assert tc.comments_truncated == 0

    def test_totally_empty_payload_does_not_crash(self):
        tc = distill_ticket({})
        assert isinstance(tc, TicketContext)
        assert tc.key == ""
        assert tc.comments == [] and tc.links == [] and tc.status_history == []

    def test_link_without_inward_or_outward_issue_is_skipped(self):
        """A malformed link entry (neither inwardIssue nor
        outwardIssue) is skipped rather than crashing the distill."""
        raw = {
            "key": "DEMO-4",
            "fields": {"issuelinks": [
                {"id": "1", "type": {"name": "Relates"}},  # no issue ref
                {"id": "2", "type": {"name": "Blocks", "outward": "blocks"},
                 "outwardIssue": {"key": "DEMO-5",
                                  "fields": {"summary": "real one",
                                             "status": {"name": "Open"}}}},
            ]},
        }
        tc = distill_ticket(raw)
        assert len(tc.links) == 1
        assert tc.links[0].key == "DEMO-5"


# ── Graceful degradation: provider with no JIRA_TOKEN ───────────────

class TestGracefulDegradation:

    def test_unconfigured_provider_returns_sentinel_not_raises(self, monkeypatch):
        """No JIRA_TOKEN → the provider still constructs, and
        fetch_ticket returns a configured=False TicketContext with a
        'not configured' note rather than raising or doing I/O. This
        is what keeps diff-graph runnable standalone."""
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        p = JiraProvider(token="")
        assert p.configured is False
        tc = p.fetch_ticket("DEMO-9")
        assert tc.configured is False
        assert tc.key == "DEMO-9"
        assert "not configured" in tc.note.lower()
        # No comments / links fabricated — it's an honest empty.
        assert tc.comments == [] and tc.links == []

    def test_configured_flag_tracks_token_presence(self, monkeypatch):
        monkeypatch.setenv("JIRA_TOKEN", "pat-xxxxx")
        assert JiraProvider().configured is True
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        assert JiraProvider().configured is False


# ── Resilience: Jira reachable but the ticket isn't ─────────────────

class TestUnviewableTicket:
    """"You can't view this issue — it may have been deleted or you
    don't have permission." Jira IS configured, but a specific key
    404s (deleted / never existed) or 403s (the bot account lacks
    permission). That's a normal per-PR condition, not a crash: the
    reviewer must get a clean note and carry on with the diff. With
    `read_ticket` heading into the reviewer's base toolset, this path
    runs on real production reviews."""

    def test_fetch_ticket_returns_sentinel_on_network_error(self, monkeypatch):
        """A configured provider whose network call raises (the
        404/403 shape `atlassian.Jira` turns into an HTTPError) →
        fetch_ticket returns a `_not_viewable` sentinel, never
        propagates the exception."""
        monkeypatch.setenv("JIRA_TOKEN", "pat-xxxxx")
        monkeypatch.delenv("DIFFGRAPH_JIRA_FIXTURE", raising=False)
        p = JiraProvider()

        def _boom(key):
            # What `atlassian.Jira.issue()` does under the hood on a
            # 404/403: response.raise_for_status() → HTTPError.
            import requests
            raise requests.exceptions.HTTPError(
                "404 Client Error: Not Found — Issue does not exist or "
                "you do not have permission to see it."
            )

        monkeypatch.setattr(p, "fetch_ticket_raw", _boom)
        tc = p.fetch_ticket("DEMO-404")
        # Did NOT raise; came back as a clean sentinel.
        assert isinstance(tc, TicketContext)
        assert tc.key == "DEMO-404"
        # configured stays True — Jira itself is fine, just this
        # ticket isn't; distinct from the no-token case.
        assert tc.configured is True
        assert tc.summary == "" and tc.comments == [] and tc.links == []
        note = tc.note.lower()
        assert "could not be read" in note
        assert "deleted" in note or "permission" in note

    def test_fixture_mode_still_fails_loud_on_missing_file(self, monkeypatch):
        """Regression guard: the network-mode catch must NOT swallow
        fixture-mode errors. A missing DIFFGRAPH_JIRA_FIXTURE is a
        test misconfiguration and must still raise — silently
        degrading it to a sentinel would hide broken scenarios."""
        monkeypatch.setenv("DIFFGRAPH_JIRA_FIXTURE", "/nonexistent/jira.json")
        p = JiraProvider()
        assert p.configured is True   # a fixture path counts as configured
        with pytest.raises(FileNotFoundError):
            p.fetch_ticket("DEMO-1")

    def test_format_ticket_renders_unviewable_note_as_one_liner(self):
        """`format_ticket` of a not-viewable context → the clean
        `[ticket KEY] <note>` one-liner, not an empty TICKET block
        with blank summary/status."""
        tc = _not_viewable("DEMO-404", RuntimeError("403"))
        out = format_ticket(tc)
        assert out.startswith("[ticket DEMO-404]")
        assert "could not be read" in out
        # Not the full-render path — no "TICKET … — … — …" header.
        assert "TICKET DEMO-404" not in out

    def test_read_ticket_tool_returns_clean_text_not_exception(self, monkeypatch):
        """End-to-end at the tool boundary: a configured Jira that
        errors on the key → the `read_ticket` tool returns a clean
        text result the agent can read and move on from. Never a
        raised exception, never a stack-tracey blob."""
        monkeypatch.setenv("JIRA_TOKEN", "pat-xxxxx")
        monkeypatch.delenv("DIFFGRAPH_JIRA_FIXTURE", raising=False)

        def _boom(self, key):
            raise ConnectionError("jira unreachable")

        monkeypatch.setattr(JiraProvider, "fetch_ticket_raw", _boom)

        from orchestra.tools.registry import ToolRegistry
        from diffgraph.orchestra_tools import register_diffgraph_tools

        class _CtxStub:
            pass

        reg = ToolRegistry()
        register_diffgraph_tools(reg, _CtxStub())
        out = reg.dispatch("read_ticket", {"ref": "default/ORD/ORD-301"})
        assert isinstance(out, str)
        # Clean note, agent-facing — surfaced via the _not_viewable
        # sentinel + format_ticket, so it reads like a ticket note,
        # not "(read_ticket failed: …)".
        assert "[ticket ORD-301]" in out
        assert "could not be read" in out


# ── Resilience: the disable-jira prod toggle ────────────────────────

class TestJiraDisabledToggle:
    """`orchestra/fixtures/mocks/disable-jira.yaml` — the operator
    opt-out. Passed via `cli.py run --mocks`, it short-circuits every
    `read_ticket` call (at any spawn depth — ToolMocks is inherited
    parent→child) with a "Jira is off, work from the diff" reply.
    These tests pin the fixture itself; the agent-keeps-going
    behaviour is scenario-tier (cf. REV-U-006 for set_review_status).
    The sticky-replay mechanism is pinned in test_tool_mocks_sticky."""

    def test_fixture_loads_as_a_sticky_read_ticket_entry(self):
        from orchestra.tool_mocks import ToolMocks
        mocks = ToolMocks.from_yaml(_DISABLE_JIRA_FIXTURE)
        assert mocks.has("read_ticket")
        entries = mocks.by_tool["read_ticket"]
        assert len(entries) == 1
        assert entries[0].sticky is True
        assert "disabled" in entries[0].return_data.lower()

    def test_read_ticket_replays_disabled_reply_indefinitely(self):
        """Sticky → however many times the reviewer (and any agent it
        spawned) calls read_ticket, it keeps getting the disabled
        reply. No MockExhaustedError."""
        from orchestra.tool_mocks import ToolMocks
        mocks = ToolMocks.from_yaml(_DISABLE_JIRA_FIXTURE)
        seen = {
            mocks.consume("read_ticket", {"ref": f"X/Y/Z-{i}"}).return_data
            for i in range(40)
        }
        assert len(seen) == 1                       # same reply every call
        assert "proceed with the PR diff" in seen.pop()
