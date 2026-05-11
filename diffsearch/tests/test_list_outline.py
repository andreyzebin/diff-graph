"""
Tests for list_files_vfs and read_outline_vfs tools.

Run with `pytest diffsearch/tests/test_list_outline.py -v --log-cli-level=INFO`
"""
from __future__ import annotations

import logging
import re
import shutil

import pytest

from diffsearch.virtual_fs import materialize_vfs
from diffsearch.tools import list_files_vfs, read_outline_vfs, _fmt_bytes

log = logging.getLogger(__name__)


def _parse_rows(text: str) -> list[dict]:
    """Parse list_files_vfs output rows into structured dicts.

    Row shape: `<status>  <path-padded>  (<summary>)`. Status is a
    single char (A/M/D/R/C/T or space). Summary either
    `<N>L · +<a>/-<b> · <bytes>` for changed files or `<N>L · <bytes>`
    for unchanged ones (or `(binary)` for binary).
    """
    rows: list[dict] = []
    for line in text.splitlines():
        if not line:
            continue
        status = line[0]
        rest = line[3:]  # skip `<X>  `
        # Path runs until the last `  (` (two spaces + paren).
        cut = rest.rfind("  (")
        if cut < 0:
            rows.append({"status": status, "path": rest.rstrip(),
                         "raw": line, "summary": None})
            continue
        path = rest[:cut].rstrip()
        summary_str = rest[cut + 2:]  # keep the leading `(`
        d = {"status": status, "path": path, "raw": line,
             "summary": summary_str, "binary": summary_str == "(binary)"}
        m = re.match(
            r"\((\d+)L(?: · \+(\d+)/-(\d+))? · ([\d.]+)([kM]?B)\)$",
            summary_str,
        )
        if m:
            d["L"] = int(m.group(1))
            d["plus"] = int(m.group(2)) if m.group(2) else 0
            d["minus"] = int(m.group(3)) if m.group(3) else 0
            d["has_changes_field"] = m.group(2) is not None
            d["size_num"] = float(m.group(4))
            d["size_unit"] = m.group(5)
        rows.append(d)
    return rows


class TestListFilesVFS:

    def test_lists_all_files(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            log.info("list_files →\n%s", out)
            assert "M  src/OrderService.java" in out
            assert "src/Util.java" in out  # unchanged context file present
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_returns_text(self, rename_field_repo):
        """Tool returns a single plain-text string (one row per file),
        not a list — matches diff_read_file / diff_search shape."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            assert isinstance(out, str)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_excludes_diffmeta(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            assert ".diffmeta" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_glob_filter(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs, "**/*.java")
            rows = _parse_rows(out)
            assert rows, "expected at least one java row"
            assert all(r["path"].endswith(".java") for r in rows)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_new_file_listed(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            assert "A  src/AuditLog.java" in out
            assert "src/Order.java" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_renamed_file_has_new_name(self, renamed_file_repo):
        repo, base, source = renamed_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            log.info("list_files (renamed) →\n%s", out)
            assert "R  src/OrderUtils.java" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_empty_when_glob_misses(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            assert list_files_vfs(vfs, "**/*.does-not-exist") == ""
        finally:
            shutil.rmtree(vfs, ignore_errors=True)


class TestListFilesStatusMarkers:
    """Status code per file mirrors the +/-/space line markers
    elsewhere — pinned per fixture so a refactor that collapses a
    status (e.g. rename → modified) is caught."""

    def _by_path(self, out: str) -> dict[str, str]:
        return {r["path"]: r["status"] for r in _parse_rows(out)}

    def test_modified(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/OrderService.java"] == "M"
            assert status["src/Util.java"] == " "
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
        """Rename surfaces as D <old> + R <new>, matching the VFS
        layout (both paths exist — old as deleted content, new as
        renamed content)."""
        repo, base, source = renamed_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            status = self._by_path(list_files_vfs(vfs))
            assert status["src/OrderHelper.java"] == "D"
            assert status["src/OrderUtils.java"] == "R"
            assert status["src/Main.java"] == " "
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_row_shape_invariant(self, any_repo):
        """Every row across every fixture parses cleanly: a single-char
        status, a path, and either `(...L · ...)` summary or `(binary)`."""
        name, repo, base, source = any_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            rows = _parse_rows(list_files_vfs(vfs))
            assert rows, f"{name}: no rows"
            valid_status = {"A", "M", "D", "R", "C", "T", " "}
            for r in rows:
                assert r["status"] in valid_status, f"{name}: {r}"
                assert r["path"], f"{name}: empty path: {r}"
                assert r["summary"] is not None, f"{name}: no summary: {r}"
                assert r["binary"] or "L" in r, f"{name}: bad summary: {r}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)


class TestListFilesSummary:
    """The `(<L>L · +N/-M · <bytes>)` trailer lets the agent budget
    reads before calling diff_read_file. These tests pin the meaning
    of each field."""

    def _row(self, out: str, path: str) -> dict:
        for r in _parse_rows(out):
            if r["path"] == path:
                return r
        raise AssertionError(f"path not in listing: {path}\n{out}")

    def test_modified_has_plus_minus(self, rename_field_repo):
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/OrderService.java")
            assert r["has_changes_field"]
            # Modified file: both sides should be non-zero (real edit).
            assert r["plus"] > 0
            assert r["minus"] > 0
            # L must be at least plus+minus (context lines add to L too,
            # so it's a lower bound, not equality).
            assert r["L"] >= r["plus"] + r["minus"]
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_added_file_all_plus(self, new_file_repo):
        repo, base, source = new_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/AuditLog.java")
            assert r["has_changes_field"]
            assert r["minus"] == 0
            assert r["plus"] == r["L"], \
                f"added file: every line is `+`, expected plus==L: {r}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_deleted_file_all_minus(self, deleted_file_repo):
        repo, base, source = deleted_file_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/LegacyService.java")
            assert r["has_changes_field"]
            assert r["plus"] == 0
            assert r["minus"] == r["L"]
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_unchanged_has_no_changes_field(self, rename_field_repo):
        """Unchanged context files (status=` `) get `(<L>L · <bytes>)`
        — no `+N/-M` clause, since both would be zero by definition."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/Util.java")
            assert r["status"] == " "
            assert not r["has_changes_field"], \
                f"unchanged file should not have +/- field: {r}"
            assert r["L"] > 0
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_L_matches_diffmeta_length(self, rename_field_repo):
        """`L` for a changed file is the number of lines in the
        unified-diff view — exactly `len(.diffmeta/<path>.json)`."""
        from diffsearch.virtual_fs import load_diffmeta
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/OrderService.java")
            meta = load_diffmeta(vfs, "src/OrderService.java")
            assert meta is not None
            assert r["L"] == len(meta)
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_plus_minus_match_meta_markers(self, rename_field_repo):
        from diffsearch.virtual_fs import load_diffmeta
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            r = self._row(list_files_vfs(vfs), "src/OrderService.java")
            meta = load_diffmeta(vfs, "src/OrderService.java")
            assert r["plus"] == sum(1 for m in meta if m["marker"] == "+")
            assert r["minus"] == sum(1 for m in meta if m["marker"] == "-")
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_columns_aligned(self, rename_field_repo):
        """The trailing `(` for every row should sit at the same
        column — that's the whole point of the path padding."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = list_files_vfs(vfs)
            paren_cols = {ln.index("  (") for ln in out.splitlines()}
            assert len(paren_cols) == 1, \
                f"expected one paren column, got {paren_cols}:\n{out}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)


class TestFmtBytes:
    @pytest.mark.parametrize("n,expected", [
        (0, "0B"),
        (1, "1B"),
        (1023, "1023B"),
        (1024, "1.0kB"),
        (1536, "1.5kB"),
        (1024 * 1024, "1.0MB"),
        (5 * 1024 * 1024 + 500 * 1024, "5.5MB"),
    ])
    def test_thresholds(self, n, expected):
        assert _fmt_bytes(n) == expected


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
