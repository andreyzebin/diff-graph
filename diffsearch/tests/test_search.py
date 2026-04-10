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


def _log_hits(label, hits):
    log.info("search '%s' → %d hits:", label, len(hits))
    for h in hits:
        old_s = f"old:{h.old}" if h.old else "     "
        new_s = f"new:{h.new}" if h.new else "     "
        log.info("  %s L%d %s %s %s %s", h.file, h.L, old_s, new_s, h.marker, h.snippet[:60])


class TestSearchVFS:

    def _make_vfs(self, repo, base, source):
        return materialize_vfs(repo, base, source)

    def test_finds_added_code(self, rename_field_repo):
        """Search finds code that was added (+ lines)."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "InventoryService")
            _log_hits("InventoryService", hits)
            assert len(hits) > 0
            service_hits = [h for h in hits if h.file == "src/OrderService.java"]
            assert any(h.marker == "+" for h in service_hits)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_finds_deleted_code(self, rename_field_repo):
        """Search finds code that was deleted (- lines)."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "InventoryClient")
            _log_hits("InventoryClient", hits)
            assert len(hits) > 0
            client_hits = [h for h in hits if h.file == "src/OrderService.java"]
            assert any(h.marker == "-" for h in client_hits)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_finds_unchanged_code(self, rename_field_repo):
        """Search finds code in unchanged files."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "format")
            util_hits = [h for h in hits if h.file == "src/Util.java"]
            assert len(util_hits) > 0
            for h in util_hits:
                assert h.L == h.old == h.new
                assert h.marker == " "
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_deleted_hit_has_old_no_new(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "InventoryClient")
            for h in hits:
                if h.marker == "-":
                    assert h.old is not None
                    assert h.new is None
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_added_hit_has_new_no_old(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "getOrder")
            assert len(hits) > 0
            for h in hits:
                if h.marker == "+":
                    assert h.new is not None
                    assert h.old is None
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_new_file(self, new_file_repo):
        """Search finds content in newly added files."""
        repo, base, source = new_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "AuditLog")
            assert len(hits) > 0
            audit_hits = [h for h in hits if h.file == "src/AuditLog.java"]
            assert len(audit_hits) > 0
            assert all(h.marker == "+" for h in audit_hits)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_split_method(self, split_method_repo):
        """Search finds both deleted processOrder and added validateOrder."""
        repo, base, source = split_method_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "Order")
            _log_hits("Order (split_method)", hits)
            markers = {h.marker for h in hits}
            assert "+" in markers
            assert "-" in markers
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_respects_max_results(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "order", max_results=3)
            assert len(hits) <= 3
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_no_diffmeta_in_results(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "marker")
            for h in hits:
                assert not h.file.startswith(".diffmeta")
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_finds_old_class_name(self, renamed_file_repo):
        """Search finds old class name in deleted content."""
        repo, base, source = renamed_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "OrderHelper")
            _log_hits("OrderHelper", hits)
            assert len(hits) > 0
            assert all(h.marker == "-" for h in hits)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_search_finds_new_method(self, renamed_file_repo):
        """Search finds new method in renamed file."""
        repo, base, source = renamed_file_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            hits = search_vfs(vfs, "formatTotal")
            _log_hits("formatTotal", hits)
            assert len(hits) > 0
            assert all(h.marker == "+" for h in hits)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
