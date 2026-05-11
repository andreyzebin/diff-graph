"""
Tests for list_files_vfs and read_outline_vfs tools.

Run with `pytest diffsearch/tests/test_list_outline.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging
import shutil

import pytest

from diffsearch.virtual_fs import materialize_vfs
from diffsearch.tools import list_files_vfs, read_outline_vfs

log = logging.getLogger(__name__)


class TestListFilesVFS:

    def test_lists_all_files(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            log.info("list_files → %s", files)
            assert "M src/OrderService.java" in files
            assert "  src/Util.java" in files
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_excludes_diffmeta(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            # Each entry is "<STATUS> <path>" — strip the prefix before checking.
            assert all(not f[2:].startswith(".diffmeta") for f in files)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_glob_filter(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs, "**/*.java")
            assert files, "expected at least one java file"
            assert all(f.endswith(".java") for f in files)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_new_file_listed(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            assert "A src/AuditLog.java" in files
            assert "  src/Order.java" in files
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_list_files_has_new_name(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            log.info("list_files (renamed) → %s", files)
            assert "R src/OrderUtils.java" in files
        finally:
            shutil.rmtree(vfs, ignore_errors=True)


class TestListFilesStatusMarkers:
    """Each entry must start with a single-char git status code plus a space,
    mirroring the +/-/space line markers used by diff_read_file / diff_search:

        A  added
        M  modified
        D  deleted
        R  rename destination (the old path appears with a D marker)
        C  copy destination
           unchanged (space)

    These tests pin every status code to a fixture so a future refactor
    that drops a status (e.g. collapses rename → modified) is caught.
    """

    def _by_path(self, files: list[str]) -> dict[str, str]:
        """`{'src/Foo.java': 'M', ...}` for assertions."""
        out: dict[str, str] = {}
        for entry in files:
            assert len(entry) >= 2 and entry[1] == " ", entry
            out[entry[2:]] = entry[0]
        return out

    def test_modified(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/OrderService.java"] == "M"
            assert status["src/Util.java"] == " "  # unchanged context file
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_added(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/AuditLog.java"] == "A"
            assert status["src/Order.java"] == " "
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_deleted(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/LegacyService.java"] == "D"
            assert status["src/Order.java"] == " "
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_renamed_pair(self, renamed_file_repo):
        """A rename surfaces as D for the old path + R for the new path,
        matching the VFS layout (both paths exist in the materialised tree
        — old as a deleted file, new as the renamed content)."""
        repo, base, source = renamed_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/OrderHelper.java"] == "D", \
                f"expected old path marked D, got: {status}"
            assert status["src/OrderUtils.java"] == "R", \
                f"expected new path marked R, got: {status}"
            assert status["src/Main.java"] == " "
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_entry_shape(self, any_repo):
        """Every entry across every fixture has shape '<X> <path>' with
        X in the known status set. Guards against accidental regressions
        where list_files_vfs forgets the prefix on some code path."""
        name, repo, base, source = any_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            assert files, f"{name}: no files listed"
            valid = {"A", "M", "D", "R", "C", "T", " "}
            for entry in files:
                assert len(entry) >= 3, f"{name}: too short: {entry!r}"
                assert entry[1] == " ", f"{name}: bad separator: {entry!r}"
                assert entry[0] in valid, f"{name}: unknown status: {entry!r}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)


class TestReadOutlineVFS:

    def test_outline_extracts_symbols(self, rename_field_repo):
        """tree-sitter must actually parse — `(tree-sitter unavailable)`
        in the output means the parser stack is broken (typically a
        diff-graph install where the per-language packages aren't
        installed or the wrong tree-sitter version is loaded). This
        used to be a tolerant 'something is returned' check, which
        silently masked a real regression — diff_outline producing
        '(tree-sitter unavailable)' on every Java file under bench
        run-unit. Strict assertion: the class name appears AND the
        unavailable sentinel does NOT.
        """
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = read_outline_vfs(vfs, "src/OrderService.java", repo_path=repo)
            log.info("outline OrderService.java:\n%s", out)
            assert len(out) > 0
            assert "tree-sitter unavailable" not in out, \
                f"tree-sitter parser not loaded — output:\n{out}"
            assert "OrderService" in out, \
                f"class name missing from outline:\n{out}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_outline_file_not_found(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = read_outline_vfs(vfs, "nonexistent.java")
            assert "not found" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
