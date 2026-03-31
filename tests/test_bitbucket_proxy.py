"""
Tests for RealBitbucketPRProxy.

The atlassian Bitbucket client is injected via the constructor and replaced
with a MagicMock so no real HTTP calls are made.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bitbucket.real_factory import RealBitbucketPRProxy


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return MagicMock()


@pytest.fixture
def proxy(client):
    return RealBitbucketPRProxy(
        client=client,
        project="PROJ",
        repo="myrepo",
        _pr_id=42,
        agent_username="agent-bot",
    )


# ── get_comments ─────────────────────────────────────────────────────────────

def _setup_comments(client, items):
    """Wire client to return *items* from the paged comments endpoint."""
    client._url_pull_request_comments.return_value = "/fake/comments/url"
    client._get_paged.return_value = iter(items)


async def test_get_comments_returns_only_agent_comments(proxy, client):
    _setup_comments(client, [
        {
            "id": 1,
            "text": "NPE risk on line 10",
            "author": {"slug": "agent-bot"},
            "anchor": {"path": "src/Foo.java", "line": 10, "lineType": "ADDED"},
            "severity": "NORMAL",
        },
        {
            "id": 2,
            "text": "I agree",
            "author": {"slug": "john-doe"},
            "anchor": None,
            "severity": "NORMAL",
        },
    ])

    comments = await proxy.get_comments()

    assert len(comments) == 1
    assert comments[0].id == 1
    assert comments[0].text == "NPE risk on line 10"
    assert comments[0].anchor is not None
    assert comments[0].anchor.path == "src/Foo.java"
    assert comments[0].anchor.line == 10
    assert comments[0].anchor.line_type == "ADDED"


async def test_get_comments_general_comment_has_no_anchor(proxy, client):
    _setup_comments(client, [
        {
            "id": 5,
            "text": "Overall LGTM",
            "author": {"slug": "agent-bot"},
            "anchor": None,
            "severity": "BLOCKER",
        }
    ])

    comments = await proxy.get_comments()

    assert len(comments) == 1
    assert comments[0].anchor is None
    assert comments[0].severity == "BLOCKER"


async def test_get_comments_empty_when_no_agent_comments(proxy, client):
    _setup_comments(client, [
        {"id": 1, "text": "Human comment", "author": {"slug": "alice"}, "anchor": None, "severity": "NORMAL"},
        {"id": 2, "text": "Another human", "author": {"slug": "bob"}, "anchor": None, "severity": "NORMAL"},
    ])

    comments = await proxy.get_comments()

    assert comments == []


async def test_get_comments_empty_list_from_api(proxy, client):
    _setup_comments(client, [])

    comments = await proxy.get_comments()

    assert comments == []


async def test_get_comments_calls_api_with_correct_args(proxy, client):
    _setup_comments(client, [])

    await proxy.get_comments()

    client._url_pull_request_comments.assert_called_once_with("PROJ", "myrepo", 42)
    client._get_paged.assert_called_once_with("/fake/comments/url")


# ── get_review_status ────────────────────────────────────────────────────────

async def test_get_review_status_returns_agent_needs_work(proxy, client):
    client.get.return_value = {
        "values": [
            {"user": {"slug": "john-doe"}, "status": "APPROVED"},
            {"user": {"slug": "agent-bot"}, "status": "NEEDS_WORK"},
        ]
    }

    status = await proxy.get_review_status()

    assert status is not None
    assert status.status == "NEEDS_WORK"


async def test_get_review_status_returns_agent_approved(proxy, client):
    client.get.return_value = {
        "values": [
            {"user": {"slug": "agent-bot"}, "status": "APPROVED"},
        ]
    }

    status = await proxy.get_review_status()

    assert status is not None
    assert status.status == "APPROVED"


async def test_get_review_status_none_when_agent_not_participant(proxy, client):
    client.get.return_value = {
        "values": [
            {"user": {"slug": "john-doe"}, "status": "APPROVED"},
        ]
    }

    status = await proxy.get_review_status()

    assert status is None


async def test_get_review_status_none_when_agent_unapproved(proxy, client):
    client.get.return_value = {
        "values": [
            {"user": {"slug": "agent-bot"}, "status": "UNAPPROVED"},
        ]
    }

    status = await proxy.get_review_status()

    assert status is None


async def test_get_review_status_none_when_empty(proxy, client):
    client.get.return_value = {"values": []}

    status = await proxy.get_review_status()

    assert status is None


async def test_get_review_status_calls_participants_endpoint(proxy, client):
    client.get.return_value = {"values": []}

    await proxy.get_review_status()

    expected_url = (
        "rest/api/1.0/projects/PROJ/repos/myrepo/pull-requests/42/participants"
    )
    client.get.assert_called_once_with(expected_url)


# ── close ────────────────────────────────────────────────────────────────────

async def test_close_declines_pr_with_current_version(proxy, client):
    client.get_pull_request.return_value = {"id": 42, "version": 3}

    await proxy.close()

    client.get_pull_request.assert_called_once_with("PROJ", "myrepo", 42)
    client.decline_pull_request.assert_called_once_with("PROJ", "myrepo", 42, 3)


async def test_close_uses_version_zero_when_missing(proxy, client):
    client.get_pull_request.return_value = {"id": 42}

    await proxy.close()

    client.decline_pull_request.assert_called_once_with("PROJ", "myrepo", 42, 0)


# ── pr_id property ───────────────────────────────────────────────────────────

def test_pr_id_property(proxy):
    assert proxy.pr_id == 42
