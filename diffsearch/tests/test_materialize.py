"""
Tests for VFS materialization to disk.

Run with `pytest diffsearch/tests/test_materialize.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import shutil
from pathlib import Path

from diffsearch.virtual_fs import materialize_vfs, load_diffmeta


class TestMaterializeVFS:

    def test_changed_file_exists(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            assert (Path(vfs) / "src/OrderService.java").exists()
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_changed_file_no_markers_in_content(self, rename_field_repo):
        """Materialized file has no +/- prefixes — it's searchable plain text."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            content = (Path(vfs) / "src/OrderService.java").read_text()
            assert "InventoryClient" in content
            assert "InventoryService" in content
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_unchanged_file_exists(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            assert (Path(vfs) / "src/Util.java").exists()
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_metadata_exists_for_changed(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            meta = load_diffmeta(vfs, "src/OrderService.java")
            assert meta is not None
            assert len(meta) > 0
            for m in meta:
                assert "L" in m
                assert "marker" in m
                assert m["marker"] in ("+", "-", " ")
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_no_metadata_for_unchanged(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            meta = load_diffmeta(vfs, "src/Util.java")
            assert meta is None
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_new_file_materialized(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            assert (Path(vfs) / "src/AuditLog.java").exists()
            meta = load_diffmeta(vfs, "src/AuditLog.java")
            assert meta is not None
            assert all(m["marker"] == "+" for m in meta)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
