import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from fake_servers.bitbucket import create_bitbucket_app
from fake_servers.jira import create_jira_app
from fake_servers.write_sink import InMemoryWriteSink
from fake_servers.providers.fixture import FixtureBitbucketProvider, FixtureJiraProvider

BB_DATA = {
    "pull_request": {
        "id": 42,
        "title": "Test PR",
        "description": "Desc",
        "author": {"name": "user", "display_name": "User"},
        "from_branch": "feature/x",
        "to_branch": "main",
        "status": "OPEN",
        "head_commit": "abc123",
    },
    "diff": [
        {
            "path": "src/Foo.java",
            "change_type": "MODIFY",
            "hunks": [
                {"old_start": 1, "new_start": 1, "lines": ["+new line"]}
            ],
        }
    ],
    "codebase_context": [
        {"path": "src/Bar.java", "content": "class Bar {}"},
    ],
}

JIRA_DATA = {
    "issue": {
        "key": "PROJ-1",
        "summary": "Test",
        "description": "Desc",
        "issuetype": "Task",
        "status": "Open",
        "labels": [],
    },
    "comments": [],
}


def make_bb_client():
    provider = FixtureBitbucketProvider(BB_DATA)
    sink = InMemoryWriteSink()
    app = create_bitbucket_app(provider, sink)
    return TestClient(app), sink


def make_jira_client():
    provider = FixtureJiraProvider(JIRA_DATA)
    app = create_jira_app(provider)
    return TestClient(app)


class TestFakeBitbucket:
    def test_get_pull_request(self):
        client, _ = make_bb_client()
        resp = client.get("/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 42
        assert data["title"] == "Test PR"
        assert data["state"] == "OPEN"

    def test_get_diff(self):
        client, _ = make_bb_client()
        resp = client.get("/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert "diffs" in data
        assert len(data["diffs"]) == 1

    def test_get_file_found(self):
        client, _ = make_bb_client()
        resp = client.get(
            "/rest/api/1.0/projects/PROJ/repos/repo/browse/src/Bar.java",
            params={"at": "abc123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "lines" in data

    def test_get_file_not_found(self):
        client, _ = make_bb_client()
        resp = client.get(
            "/rest/api/1.0/projects/PROJ/repos/repo/browse/src/Missing.java",
            params={"at": "abc123"},
        )
        assert resp.status_code == 404

    def test_add_general_comment(self):
        client, sink = make_bb_client()
        resp = client.post(
            "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/comments",
            json={"text": "Looks good"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["text"] == "Looks good"
        assert data["anchor"] is None

    def test_add_inline_comment(self):
        client, sink = make_bb_client()
        resp = client.post(
            "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/comments",
            json={
                "text": "Bug here",
                "anchor": {"path": "src/Foo.java", "line": 10, "lineType": "ADDED"},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["anchor"]["path"] == "src/Foo.java"

    def test_set_participants_needs_work(self):
        client, _ = make_bb_client()
        resp = client.post(
            "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/participants",
            json={"status": "NEEDS_WORK"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "NEEDS_WORK"

    def test_benchmark_captured(self):
        client, _ = make_bb_client()
        client.post(
            "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/comments",
            json={"text": "Comment"},
        )
        resp = client.get("/_benchmark/captured")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["comments"]) == 1

    def test_benchmark_reset(self):
        client, _ = make_bb_client()
        client.post(
            "/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/comments",
            json={"text": "Comment"},
        )
        client.post("/_benchmark/reset")
        resp = client.get("/_benchmark/captured")
        assert resp.json()["comments"] == []


class TestFakeJira:
    def test_get_issue(self):
        client = make_jira_client()
        resp = client.get("/rest/api/2/issue/PROJ-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "PROJ-1"
        assert data["fields"]["summary"] == "Test"

    def test_get_comments(self):
        client = make_jira_client()
        resp = client.get("/rest/api/2/issue/PROJ-1/comment")
        assert resp.status_code == 200
        data = resp.json()
        assert "comments" in data
        assert data["total"] == 0

    def test_get_unknown_issue(self):
        provider = FixtureJiraProvider({})
        app = create_jira_app(provider)
        client = TestClient(app)
        resp = client.get("/rest/api/2/issue/PROJ-999")
        assert resp.status_code == 404
