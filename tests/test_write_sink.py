import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fake_servers.write_sink import InMemoryWriteSink
from fake_servers.providers.base import CommentAnchor


@pytest.mark.asyncio
async def test_add_general_comment():
    sink = InMemoryWriteSink()
    thread = await sink.add_comment(pr_id=1, text="General comment", anchor=None)
    assert thread.id > 0
    assert thread.text == "General comment"
    assert thread.anchor is None


@pytest.mark.asyncio
async def test_add_inline_comment():
    sink = InMemoryWriteSink()
    anchor = CommentAnchor(path="src/Foo.java", line=42, line_type="ADDED")
    thread = await sink.add_comment(pr_id=1, text="Inline comment", anchor=anchor)
    assert thread.anchor is not None
    assert thread.anchor.path == "src/Foo.java"
    assert thread.anchor.line == 42


@pytest.mark.asyncio
async def test_set_review_status():
    sink = InMemoryWriteSink()
    rs = await sink.set_review_status(pr_id=1, status="NEEDS_WORK")
    assert rs.status == "NEEDS_WORK"


@pytest.mark.asyncio
async def test_get_captured_empty():
    sink = InMemoryWriteSink()
    captured = await sink.get_captured()
    assert captured.comments == []
    assert captured.review_status is None


@pytest.mark.asyncio
async def test_get_captured_with_data():
    sink = InMemoryWriteSink()
    anchor = CommentAnchor(path="file.py", line=10, line_type="ADDED")
    await sink.add_comment(1, "Inline", anchor)
    await sink.add_comment(1, "General", None)
    await sink.set_review_status(1, "APPROVED")

    captured = await sink.get_captured()
    assert len(captured.comments) == 2
    assert len(captured.inline_comments) == 1
    assert len(captured.general_comments) == 1
    assert captured.review_status is not None
    assert captured.review_status.status == "APPROVED"


@pytest.mark.asyncio
async def test_reset_clears_state():
    sink = InMemoryWriteSink()
    await sink.add_comment(1, "Comment", None)
    await sink.set_review_status(1, "APPROVED")

    await sink.reset()

    captured = await sink.get_captured()
    assert captured.comments == []
    assert captured.review_status is None


@pytest.mark.asyncio
async def test_comments_get_unique_ids():
    sink = InMemoryWriteSink()
    t1 = await sink.add_comment(1, "First", None)
    t2 = await sink.add_comment(1, "Second", None)
    assert t1.id != t2.id
