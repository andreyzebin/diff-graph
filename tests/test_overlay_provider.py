import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from fake_servers.providers.overlay import (
    OverlayBitbucketProvider, BitbucketOverrides,
    OverlayJiraProvider, JiraOverrides,
)
from fake_servers.providers.base import (
    PullRequestData, Author, FileDiff, DiffHunk, FileContent,
    IssueData, JiraComment,
)


def _make_pr(pr_id: int = 1) -> PullRequestData:
    return PullRequestData(
        id=pr_id,
        title="Base PR",
        description="Base description",
        author=Author(name="base_user", display_name="Base User"),
        from_branch="feature/base",
        to_branch="main",
        status="OPEN",
        head_commit="base123",
    )


def _make_mock_base():
    base = MagicMock()
    base.get_pull_request = AsyncMock(return_value=_make_pr())
    base.get_diff = AsyncMock(return_value=[])
    base.get_file = AsyncMock(return_value=FileContent(path="src/Base.java", content="class Base {}"))
    base.current_pr_id = 1
    return base


class TestOverlayBitbucketProvider:
    @pytest.mark.asyncio
    async def test_delegates_pr_when_no_override(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({})
        provider = OverlayBitbucketProvider(base, overrides)

        pr = await provider.get_pull_request("P", "R", 1)
        assert pr.title == "Base PR"
        base.get_pull_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delegates_file_when_no_override(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({})
        provider = OverlayBitbucketProvider(base, overrides)

        f = await provider.get_file("P", "R", "src/Base.java", "HEAD")
        assert f is not None
        assert f.content == "class Base {}"
        base.get_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_overrides_file_when_present(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({
            "files": [
                {"path": "src/Base.java", "content": "class Overridden {}"}
            ]
        })
        provider = OverlayBitbucketProvider(base, overrides)

        f = await provider.get_file("P", "R", "src/Base.java", "HEAD")
        assert f is not None
        assert f.content == "class Overridden {}"
        base.get_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_through_for_non_overridden_file(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({
            "files": [
                {"path": "src/Other.java", "content": "class Other {}"}
            ]
        })
        provider = OverlayBitbucketProvider(base, overrides)

        f = await provider.get_file("P", "R", "src/Base.java", "HEAD")
        base.get_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delegates_diff_always(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({})
        provider = OverlayBitbucketProvider(base, overrides)

        await provider.get_diff("P", "R", 1)
        base.get_diff.assert_awaited_once()

    def test_current_pr_id_delegates(self):
        base = _make_mock_base()
        overrides = BitbucketOverrides({})
        provider = OverlayBitbucketProvider(base, overrides)
        assert provider.current_pr_id == 1


class TestOverlayJiraProvider:
    @pytest.mark.asyncio
    async def test_delegates_issue_when_no_override(self):
        base = MagicMock()
        base.get_issue = AsyncMock(return_value=IssueData(
            key="PROJ-1", summary="Base", description="", issuetype="Task", status="Open"
        ))
        overrides = JiraOverrides({})
        provider = OverlayJiraProvider(base, overrides)

        issue = await provider.get_issue("PROJ-1")
        assert issue.summary == "Base"
        base.get_issue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_overrides_issue_when_present(self):
        base = MagicMock()
        base.get_issue = AsyncMock()
        override_issue = IssueData(
            key="PROJ-1", summary="Overridden", description="", issuetype="Story", status="Done"
        )
        overrides = JiraOverrides({"issue": override_issue})
        provider = OverlayJiraProvider(base, overrides)

        issue = await provider.get_issue("PROJ-1")
        assert issue.summary == "Overridden"
        base.get_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delegates_comments_when_no_override(self):
        base = MagicMock()
        base.get_comments = AsyncMock(return_value=[])
        overrides = JiraOverrides({})
        provider = OverlayJiraProvider(base, overrides)

        comments = await provider.get_comments("PROJ-1")
        assert comments == []
        base.get_comments.assert_awaited_once()
