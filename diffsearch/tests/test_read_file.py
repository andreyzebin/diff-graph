"""
Tests for read_file_vfs tool.

Run with `pytest diffsearch/tests/test_read_file.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging
import shutil

from diffsearch.virtual_fs import materialize_vfs, get_changed_files
from diffsearch.tools import read_file_vfs

log = logging.getLogger(__name__)


class TestReadFileVFS:

    def _make_vfs(self, repo, base, source):
        return materialize_vfs(repo, base, source)

    def test_unchanged_plain_output(self, rename_field_repo):
        """Unchanged file looks like a normal file with line numbers."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/Util.java")
            log.info("read_file unchanged:\n%s", out)
            assert "# src/Util.java" in out
            assert "public class Util" in out
            assert "old" not in out.splitlines()[0]
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changed_shows_old_new_columns(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/OrderService.java")
            log.info("read_file changed (full):\n%s", out)
            assert "old" in out
            assert "new" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changed_shows_markers(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/OrderService.java")
            lines = out.splitlines()
            markers = [l for l in lines if "|+" in l or "|-" in l]
            assert len(markers) > 0, "expected +/- markers in output"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_start_end_slicing(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            full = read_file_vfs(vfs, "src/OrderService.java", start_line=1, end_line=999)
            partial = read_file_vfs(vfs, "src/OrderService.java", start_line=5, end_line=10)
            log.info("read_file sliced L5-L10:\n%s", partial)
            full_lines = full.splitlines()
            partial_lines = partial.splitlines()
            assert len(partial_lines) < len(full_lines)
            assert "L5-L10" in partial_lines[0] or "L5" in partial_lines[0]
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_line_numbers_false(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/OrderService.java", line_numbers=False)
            assert "old  new" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_file_not_found(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "nonexistent.java")
            assert "not found" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_read_deleted_file(self, renamed_file_repo):
        """read_file shows deleted content with old line numbers."""
        repo, base, source = renamed_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            changed = get_changed_files(base, source, repo)
            if "src/OrderHelper.java" in changed:
                out = read_file_vfs(vfs, "src/OrderHelper.java")
                log.info("read_file deleted OrderHelper:\n%s", out)
                assert "OrderHelper" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changes_only(self, rename_field_repo):
        """changes_only shows only hunks with context."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            full = read_file_vfs(vfs, "src/OrderService.java")
            diff = read_file_vfs(vfs, "src/OrderService.java", changes_only=True)
            log.info("read_file changes_only:\n%s", diff)
            # Should be shorter than full file
            assert len(diff.splitlines()) < len(full.splitlines())
            # Should contain +/- markers
            assert "|+" in diff
            assert "|-" in diff
            # Should have separators between hunks
            assert "  --" in diff
            # Should have header
            assert "changes only" in diff
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changes_only_unchanged_file(self, rename_field_repo):
        """changes_only on unchanged file returns no changes."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/Util.java", changes_only=True)
            assert "no changes" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changes_only_new_file(self, new_file_repo):
        """changes_only on new file shows all lines as +."""
        repo, base, source = new_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = read_file_vfs(vfs, "src/AuditLog.java", changes_only=True)
            log.info("read_file changes_only new file:\n%s", out)
            assert "new file" in out
            assert "|+" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
