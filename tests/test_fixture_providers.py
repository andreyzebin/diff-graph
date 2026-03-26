import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fake_servers.providers.fixture import FixtureBitbucketProvider, FixtureJiraProvider
from fake_servers.providers.base import ProviderNotFoundError

BB_DATA = {
    "pull_request": {
        "id": 42,
        "title": "Test PR",
        "description": "Test description",
        "author": {"name": "user", "display_name": "User Name"},
        "from_branch": "feature/test",
        "to_branch": "main",
        "status": "OPEN",
        "head_commit": "abc123",
    },
    "diff": [
        {
            "path": "src/Foo.java",
            "change_type": "MODIFY",
            "hunks": [
                {"old_start": 1, "new_start": 1, "lines": ["+added line", " context"]}
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
        "summary": "Test issue",
        "description": "Test description",
        "issuetype": "Story",
        "status": "In Progress",
        "labels": ["backend"],
    },
    "comments": [
        {"id": 1, "body": "Comment 1", "author": "alice"},
    ],
}


@pytest.mark.asyncio
async def test_fixture_bb_get_pull_request():
    provider = FixtureBitbucketProvider(BB_DATA)
    pr = await provider.get_pull_request("PROJ", "repo", 42)
    assert pr.id == 42
    assert pr.title == "Test PR"
    assert pr.author.name == "user"
    assert pr.from_branch == "feature/test"
    assert pr.status == "OPEN"


@pytest.mark.asyncio
async def test_fixture_bb_get_diff():
    provider = FixtureBitbucketProvider(BB_DATA)
    diffs = await provider.get_diff("PROJ", "repo", 42)
    assert len(diffs) == 1
    assert diffs[0].path == "src/Foo.java"
    assert diffs[0].change_type == "MODIFY"
    assert len(diffs[0].hunks) == 1
    assert "+added line" in diffs[0].hunks[0].lines


@pytest.mark.asyncio
async def test_fixture_bb_get_file_found():
    provider = FixtureBitbucketProvider(BB_DATA)
    file = await provider.get_file("PROJ", "repo", "src/Bar.java", "abc123")
    assert file is not None
    assert file.path == "src/Bar.java"
    assert "Bar" in file.content


@pytest.mark.asyncio
async def test_fixture_bb_get_file_not_found():
    provider = FixtureBitbucketProvider(BB_DATA)
    file = await provider.get_file("PROJ", "repo", "src/Missing.java", "abc123")
    assert file is None


@pytest.mark.asyncio
async def test_fixture_bb_current_pr_id():
    provider = FixtureBitbucketProvider(BB_DATA)
    assert provider.current_pr_id == 42


@pytest.mark.asyncio
async def test_fixture_bb_no_pr_raises():
    provider = FixtureBitbucketProvider({})
    with pytest.raises(ProviderNotFoundError):
        await provider.get_pull_request("PROJ", "repo", 1)


@pytest.mark.asyncio
async def test_fixture_jira_get_issue():
    provider = FixtureJiraProvider(JIRA_DATA)
    issue = await provider.get_issue("PROJ-1")
    assert issue.key == "PROJ-1"
    assert issue.summary == "Test issue"
    assert issue.issuetype == "Story"
    assert "backend" in issue.labels


@pytest.mark.asyncio
async def test_fixture_jira_get_comments():
    provider = FixtureJiraProvider(JIRA_DATA)
    comments = await provider.get_comments("PROJ-1")
    assert len(comments) == 1
    assert comments[0].body == "Comment 1"
    assert comments[0].author == "alice"


@pytest.mark.asyncio
async def test_fixture_jira_no_issue_raises():
    provider = FixtureJiraProvider({})
    with pytest.raises(ProviderNotFoundError):
        await provider.get_issue("PROJ-1")


@pytest.mark.asyncio
async def test_fixture_bb_pr_lifecycle_noop():
    """pr_lifecycle is a no-op for fixture provider."""
    provider = FixtureBitbucketProvider(BB_DATA)

    class FakeScenario:
        pass

    entered = False
    async with provider.pr_lifecycle(FakeScenario()):
        entered = True
    assert entered
