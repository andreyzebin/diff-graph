"""
Tests for list_files_vfs and read_outline_vfs tools.

Run with `pytest diffsearch/tests/test_list_outline.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging
import shutil

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
            assert "src/OrderService.java" in files
            assert "src/Util.java" in files
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_excludes_diffmeta(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            assert all(not f.startswith(".diffmeta") for f in files)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_glob_filter(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs, "**/*.java")
            assert all(f.endswith(".java") for f in files)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_new_file_listed(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            assert "src/AuditLog.java" in files
            assert "src/Order.java" in files
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_list_files_has_new_name(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            files = list_files_vfs(vfs)
            log.info("list_files (renamed) → %s", files)
            assert "src/OrderUtils.java" in files
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
