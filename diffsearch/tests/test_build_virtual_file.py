"""
Tests for VirtualFile construction — invariants and per-fixture content.

Run with `pytest diffsearch/tests/test_build_virtual_file.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging

from diffsearch.virtual_fs import build_virtual_file, get_changed_files

log = logging.getLogger(__name__)


class TestBuildVirtualFile:
    """Core invariants for virtual file construction (all fixtures)."""

    def test_L_monotonic(self, any_repo):
        """L numbers are 1..N with no gaps."""
        name, repo, base, source = any_repo
        changed = get_changed_files(base, source, repo)
        log.info("fixture=%s changed=%s", name, changed)
        for path in changed:
            vf = build_virtual_file(base, source, path, repo)
            log.info("--- %s: %d virtual lines ---", path, len(vf.lines))
            for l in vf.lines:
                old_s = f"old:{l.old:>3}" if l.old is not None else "       "
                new_s = f"new:{l.new:>3}" if l.new is not None else "       "
                log.info("  L%-3d %s %s %s %s", l.L, old_s, new_s, l.marker, l.content[:80])
            assert [l.L for l in vf.lines] == list(range(1, len(vf.lines) + 1))

    def test_marker_partition(self, any_repo):
        """Every line has exactly one marker: +, -, or space."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            for line in vf.lines:
                assert line.marker in ("+", "-", " "), f"bad marker: {line.marker!r}"

    def test_plus_has_new_no_old(self, any_repo):
        """Added lines have new line number but no old."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            for line in vf.lines:
                if line.marker == "+":
                    assert line.new is not None, f"L{line.L}: + line missing new"
                    assert line.old is None, f"L{line.L}: + line has old={line.old}"

    def test_minus_has_old_no_new(self, any_repo):
        """Deleted lines have old line number but no new."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            for line in vf.lines:
                if line.marker == "-":
                    assert line.old is not None, f"L{line.L}: - line missing old"
                    assert line.new is None, f"L{line.L}: - line has new={line.new}"

    def test_context_has_both(self, any_repo):
        """Context lines have both old and new line numbers."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            for line in vf.lines:
                if line.marker == " ":
                    assert line.old is not None, f"L{line.L}: context missing old"
                    assert line.new is not None, f"L{line.L}: context missing new"

    def test_mappings_bidirectional(self, any_repo):
        """L↔old and L↔new mappings are consistent inverses."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            for L, new in vf.L_to_new.items():
                assert vf.new_to_L[new] == L
            for L, old in vf.L_to_old.items():
                assert vf.old_to_L[old] == L

    def test_old_monotonic(self, any_repo):
        """Old line numbers increase monotonically."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            old_nums = [l.old for l in vf.lines if l.old is not None]
            assert old_nums == sorted(old_nums)

    def test_new_monotonic(self, any_repo):
        """New line numbers increase monotonically."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            new_nums = [l.new for l in vf.lines if l.new is not None]
            assert new_nums == sorted(new_nums)

    def test_total_L_includes_both_markers(self, any_repo):
        """Virtual file is longer than either version alone when there are changes."""
        name, repo, base, source = any_repo
        for path in get_changed_files(base, source, repo):
            vf = build_virtual_file(base, source, path, repo)
            n_plus = sum(1 for l in vf.lines if l.marker == "+")
            n_minus = sum(1 for l in vf.lines if l.marker == "-")
            if n_plus > 0 and n_minus > 0:
                max_old = max((l.old for l in vf.lines if l.old), default=0)
                max_new = max((l.new for l in vf.lines if l.new), default=0)
                assert len(vf.lines) > max(max_old, max_new)


class TestUnchangedFile:
    """Unchanged files: L == old == new, full equivalence."""

    def test_unchanged_L_equals_old_equals_new(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vf = build_virtual_file(base, source, "src/Util.java", repo)
        for line in vf.lines:
            assert line.marker == " "
            assert line.L == line.old == line.new

    def test_unchanged_no_diff_markers(self, new_file_repo):
        repo, base, source = new_file_repo
        vf = build_virtual_file(base, source, "src/Order.java", repo)
        assert all(l.marker == " " for l in vf.lines)
        assert len(vf.lines) > 0


class TestRenameFieldContent:
    """rename_field fixture: rename field + null check + new method."""

    def test_deleted_inventoryclient(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        deleted = [l for l in vf.lines if l.marker == "-"]
        assert any("InventoryClient" in l.content for l in deleted)

    def test_added_inventoryservice(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("InventoryService" in l.content for l in added)

    def test_added_null_check(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("getItems() != null" in l.content for l in added)

    def test_added_getorder_method(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("getOrder" in l.content for l in added)


class TestSplitMethodContent:
    """split_method fixture: processOrder → validateOrder + executeOrder."""

    def test_processorder_deleted(self, split_method_repo):
        repo, base, source = split_method_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        deleted = [l for l in vf.lines if l.marker == "-"]
        assert any("processOrder" in l.content for l in deleted)

    def test_validateorder_added(self, split_method_repo):
        repo, base, source = split_method_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("validateOrder" in l.content for l in added)

    def test_executeorder_added(self, split_method_repo):
        repo, base, source = split_method_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("executeOrder" in l.content for l in added)

    def test_auditlog_added(self, split_method_repo):
        repo, base, source = split_method_repo
        vf = build_virtual_file(base, source, "src/OrderService.java", repo)
        added = [l for l in vf.lines if l.marker == "+"]
        assert any("auditLog" in l.content for l in added)


class TestNewFileContent:
    """new_file fixture: AuditLog.java entirely new."""

    def test_new_file_all_plus(self, new_file_repo):
        repo, base, source = new_file_repo
        vf = build_virtual_file(base, source, "src/AuditLog.java", repo)
        assert len(vf.lines) > 0
        assert all(l.marker == "+" for l in vf.lines)

    def test_new_file_L_equals_new(self, new_file_repo):
        repo, base, source = new_file_repo
        vf = build_virtual_file(base, source, "src/AuditLog.java", repo)
        for line in vf.lines:
            assert line.L == line.new
            assert line.old is None

    def test_changed_files_includes_new(self, new_file_repo):
        repo, base, source = new_file_repo
        changed = get_changed_files(base, source, repo)
        assert "src/AuditLog.java" in changed

    def test_changed_files_excludes_unchanged(self, new_file_repo):
        repo, base, source = new_file_repo
        changed = get_changed_files(base, source, repo)
        assert "src/Order.java" not in changed


class TestDeletedFileContent:
    """deleted_file fixture: LegacyService.java removed entirely."""

    def test_deleted_in_changed_files(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        changed = get_changed_files(base, source, repo)
        log.info("deleted_file changed=%s", changed)
        assert "src/LegacyService.java" in changed

    def test_deleted_file_all_minus(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        vf = build_virtual_file(base, source, "src/LegacyService.java", repo)
        log.info("--- deleted LegacyService.java: %d virtual lines ---", len(vf.lines))
        for l in vf.lines:
            old_s = f"old:{l.old:>3}" if l.old is not None else "       "
            new_s = f"new:{l.new:>3}" if l.new is not None else "       "
            log.info("  L%-3d %s %s %s %s", l.L, old_s, new_s, l.marker, l.content[:80])
        assert len(vf.lines) > 0
        assert all(l.marker == "-" for l in vf.lines)

    def test_deleted_file_L_equals_old(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        vf = build_virtual_file(base, source, "src/LegacyService.java", repo)
        for line in vf.lines:
            assert line.L == line.old
            assert line.new is None

    def test_deleted_file_has_content(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        vf = build_virtual_file(base, source, "src/LegacyService.java", repo)
        assert any("LegacyService" in l.content for l in vf.lines)
        assert any("findOrder" in l.content for l in vf.lines)
        assert any("deleteOrder" in l.content for l in vf.lines)

    def test_unchanged_not_in_changed(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        changed = get_changed_files(base, source, repo)
        assert "src/Order.java" not in changed


class TestRenamedFileContent:
    """renamed_file fixture: OrderHelper.java → OrderUtils.java."""

    def test_renamed_in_changed_files(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        changed = get_changed_files(base, source, repo)
        log.info("renamed_file changed=%s", changed)
        has_old = "src/OrderHelper.java" in changed
        has_new = "src/OrderUtils.java" in changed
        assert has_old or has_new, f"neither old nor new path in changed: {changed}"

    def test_old_file_content_deleted(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        changed = get_changed_files(base, source, repo)
        if "src/OrderHelper.java" in changed:
            vf = build_virtual_file(base, source, "src/OrderHelper.java", repo)
            log.info("--- deleted OrderHelper.java: %d virtual lines ---", len(vf.lines))
            for l in vf.lines:
                old_s = f"old:{l.old:>3}" if l.old is not None else "       "
                new_s = f"new:{l.new:>3}" if l.new is not None else "       "
                log.info("  L%-3d %s %s %s %s", l.L, old_s, new_s, l.marker, l.content[:80])
            assert all(l.marker == "-" for l in vf.lines)
            assert any("OrderHelper" in l.content for l in vf.lines)

    def test_new_file_content_added(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        changed = get_changed_files(base, source, repo)
        if "src/OrderUtils.java" in changed:
            vf = build_virtual_file(base, source, "src/OrderUtils.java", repo)
            log.info("--- added OrderUtils.java: %d virtual lines ---", len(vf.lines))
            for l in vf.lines:
                old_s = f"old:{l.old:>3}" if l.old is not None else "       "
                new_s = f"new:{l.new:>3}" if l.new is not None else "       "
                log.info("  L%-3d %s %s %s %s", l.L, old_s, new_s, l.marker, l.content[:80])
            assert all(l.marker == "+" for l in vf.lines)
            assert any("OrderUtils" in l.content for l in vf.lines)
            assert any("formatTotal" in l.content for l in vf.lines)
