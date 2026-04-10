"""
Tests for search_vfs tool.

Run with `pytest diffsearch/tests/test_search.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging
import shutil

from diffsearch.virtual_fs import materialize_vfs
from diffsearch.tools import search_vfs

log = logging.getLogger(__name__)


class TestSearchVFS:

    def _make_vfs(self, repo, base, source):
        return materialize_vfs(repo, base, source)

    def test_finds_added_code(self, rename_field_repo):
        """Search finds code that was added (+ lines)."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "InventoryService")
            log.info("search 'InventoryService':\n%s", out)
            assert "InventoryService" in out
            assert "|+" in out  # added marker
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_finds_deleted_code(self, rename_field_repo):
        """Search finds code that was deleted (- lines)."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "InventoryClient")
            log.info("search 'InventoryClient':\n%s", out)
            assert "InventoryClient" in out
            assert "|-" in out  # deleted marker
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_finds_unchanged_code(self, rename_field_repo):
        """Search finds code in unchanged files."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "format")
            log.info("search 'format':\n%s", out)
            assert "src/Util.java" in out
            assert "format" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_deleted_hit_shows_old(self, rename_field_repo):
        """Deleted hits show old: line number."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "InventoryClient")
            assert "old:" in out
            assert "|-" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_added_hit_shows_new(self, rename_field_repo):
        """Added hits show new: line number."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "getOrder")
            log.info("search 'getOrder':\n%s", out)
            assert "new:" in out
            assert "|+" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_new_file(self, new_file_repo):
        """Search finds content in newly added files."""
        repo, base, source = new_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "AuditLog")
            log.info("search 'AuditLog':\n%s", out)
            assert "src/AuditLog.java" in out
            assert "AuditLog" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_split_method(self, split_method_repo):
        """Search finds both deleted processOrder and added validateOrder."""
        repo, base, source = split_method_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "Order")
            log.info("search 'Order' in split_method:\n%s", out)
            assert "|+" in out
            assert "|-" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_no_match(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "xyznonexistent")
            assert "no matches" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_no_diffmeta_in_results(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "marker")
            assert ".diffmeta" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_finds_old_class_name(self, renamed_file_repo):
        """Search finds old class name in deleted content."""
        repo, base, source = renamed_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "OrderHelper")
            log.info("search 'OrderHelper':\n%s", out)
            assert "OrderHelper" in out
            assert "|-" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_finds_new_method(self, renamed_file_repo):
        """Search finds new method in renamed file."""
        repo, base, source = renamed_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "formatTotal")
            log.info("search 'formatTotal':\n%s", out)
            assert "formatTotal" in out
            assert "|+" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_grouped_by_file(self, rename_field_repo):
        """Results are grouped by file — file path appears once as header."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "OrderService")
            log.info("search 'OrderService' grouped:\n%s", out)
            lines = out.splitlines()
            # First non-empty line should be a file path (no leading spaces)
            headers = [l for l in lines if l and not l.startswith(" ")]
            assert len(headers) >= 1
            assert any("src/OrderService.java" in h for h in headers)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_context_before_after(self, rename_field_repo):
        """before/after params add context lines."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            without = search_vfs(vfs, "InventoryService")
            with_ctx = search_vfs(vfs, "InventoryService", before=2, after=2)
            log.info("search with context:\n%s", with_ctx)
            # With context should have more lines
            assert len(with_ctx.splitlines()) > len(without.splitlines())
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_context_separator(self, rename_field_repo):
        """Non-adjacent context groups separated by --."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "InventoryService", before=1, after=1)
            log.info("search with separator:\n%s", out)
            # Multiple hits in same file with gap → separator
            assert "  --" in out or out.count("InventoryService") <= 2
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
