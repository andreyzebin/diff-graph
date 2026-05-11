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

    def test_search_unchanged_file_by_glob(self, rename_field_repo):
        """Search with glob targeting an unchanged file finds content."""
        repo, base, source = rename_field_repo
        vfs = self._make_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "format", glob="**/Util.java")
            log.info("search 'format' in Util.java:\n%s", out)
            assert "Util.java" in out
            assert "format" in out
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


class TestSearchVFSRegex:
    """`regex=True` must use extended regex (ERE) so `|`, `+`, `?`, `()`,
    `{n,m}` work — `grep` defaults to basic regex (BRE) where those are
    literal, and a query like `Foo|Bar` silently returns zero results.

    These tests pin the contract for callers: regex=True ⇒ ERE,
    regex=False ⇒ fixed-string (so dots and other regex metachars
    don't accidentally match).
    """

    def test_alternation_finds_either_branch(self, rename_field_repo):
        """`A|B` matches lines containing either A or B (the actual bug
        we saw in plan 102: `store.credit|storeCredit|StoreCredit` was
        returning '(no matches)' even though the diff had OrderService
        and InventoryService in it)."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "OrderService|InventoryService", regex=True)
            log.info("regex alternation result:\n%s", out)
            assert "no matches" not in out, \
                "alternation `A|B` must match either side (was BRE-broken)"
            assert "OrderService" in out or "InventoryService" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_alternation_matches_both_sides(self, rename_field_repo):
        """Both sides of an alternation produce hits when both occur."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "OrderService|InventoryService", regex=True)
            # Both names exist in this fixture — both should appear in
            # the output (grouped by file).
            assert "OrderService" in out
            assert "InventoryService" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_regex_dot_wildcard(self, rename_field_repo):
        """`.` matches any single char under ERE — `Order.ervice`
        finds `OrderService`."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "Order.ervice", regex=True)
            assert "OrderService" in out
            assert "no matches" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_regex_quantifier(self, rename_field_repo):
        """`?` (zero-or-one) is ERE-only — proves we're not on BRE."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            # `Inventory(Service|Client)?` matches "Inventory",
            # "InventoryService", or "InventoryClient" — uses both
            # alternation and `?`, ERE features.
            out = search_vfs(vfs, "Inventory(Service|Client)?", regex=True)
            assert "no matches" not in out
            assert "Inventory" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_regex_anchors(self, rename_field_repo):
        """End-of-line anchor `$` works."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            # Match a `;` at end of line — pervasive in Java.
            out = search_vfs(vfs, ";$", regex=True)
            assert "no matches" not in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_fixed_string_does_not_match_dot_as_wildcard(self, rename_field_repo):
        """regex=False ⇒ `-F` ⇒ `.` is literal. `Order.Service`
        (with a literal dot) must NOT match `OrderService`."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "Order.Service", regex=False)
            assert "no matches" in out, \
                f"fixed-string mode leaked regex semantics:\n{out}"
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_fixed_string_handles_pipe_literal(self, rename_field_repo):
        """regex=False ⇒ pipe is literal; absent from this fixture ⇒
        no matches. (Smoke test that we're really in -F mode.)"""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            out = search_vfs(vfs, "Foo|Bar", regex=False)
            assert "no matches" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)

    def test_regex_grouped_alternation_with_dots(self, rename_field_repo):
        """The exact shape the agent sent in plan 102 — three
        alternatives, one with a `.` wildcard. Must produce at least
        one hit on a Java diff."""
        repo, base, source = rename_field_repo
        vfs = materialize_vfs(repo, base, source)
        try:
            # `order.service` (dot=wildcard) ⇒ matches "orderService",
            # "OrderService" via case-insensitive grep, "Order.service"
            # in import lines, etc. Combined with two literal variants
            # via `|` — the exact shape that was silently failing.
            out = search_vfs(
                vfs,
                "order.service|OrderService|orderRepository",
                regex=True,
            )
            assert "no matches" not in out
            assert "OrderService" in out
        finally:
            shutil.rmtree(vfs, ignore_errors=True)
