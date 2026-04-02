"""Tests for mark_changed_symbols and _extract_before_code."""
import pytest
from diffgraph.diff_parser import FileDiff, HunkSnippet
from diffgraph.diffgraph import _extract_before_code, mark_changed_symbols
from diffgraph.model import MetaModel, Module, Symbol


def make_symbol(name, start, end):
    return Symbol(
        name=name, kind="METHOD", signature=f"void {name}()",
        summary="", annotations=[], start_line=start, end_line=end,
    )


def make_file_diff(path, hunks, after_changed_lines):
    return FileDiff(
        path=path, before_path=path, status="modified",
        hunks=hunks, after_changed_lines=after_changed_lines,
    )


class TestExtractBeforeCode:
    def test_overlapping_hunk_returns_before_lines(self):
        sym = make_symbol("foo", 10, 20)
        hunk = HunkSnippet(
            before_lines=["    old line 1", "    old line 2"],
            after_lines=["    new line 1"],
            before_start=10, after_start=10,
        )
        fd = make_file_diff("Foo.java", [hunk], [10])
        result = _extract_before_code(sym, fd)
        assert result == "    old line 1\n    old line 2"

    def test_non_overlapping_hunk_returns_none(self):
        sym = make_symbol("foo", 30, 40)
        hunk = HunkSnippet(
            before_lines=["    old"],
            after_lines=["    new"],
            before_start=5, after_start=5,
        )
        fd = make_file_diff("Foo.java", [hunk], [5])
        result = _extract_before_code(sym, fd)
        assert result is None

    def test_new_symbol_no_before_lines(self):
        sym = make_symbol("newMethod", 10, 20)
        hunk = HunkSnippet(
            before_lines=[],  # new code only
            after_lines=["    new line 1", "    new line 2"],
            before_start=10, after_start=10,
        )
        fd = make_file_diff("Foo.java", [hunk], [10, 11])
        result = _extract_before_code(sym, fd)
        assert result is None

    def test_pure_deletion_inside_symbol_returns_before_lines(self):
        # Hunk that starts before the symbol (context), deletion is inside
        sym = make_symbol("toJson", 41, 67)
        hunk = HunkSnippet(
            before_lines=["    if s.full_code is not None:", "        sym['full_code'] = s.full_code"],
            after_lines=[],
            before_start=53, after_start=51,
            deletion_positions=[53, 54],  # deletions at lines 53-54 inside sym
        )
        fd = make_file_diff("model.py", [hunk], [53, 54])
        result = _extract_before_code(sym, fd)
        assert result is not None
        assert "full_code" in result

    def test_pure_deletion_outside_symbol_returns_none(self):
        sym = make_symbol("add", 38, 40)
        hunk = HunkSnippet(
            before_lines=["    old line"],
            after_lines=[],
            before_start=53, after_start=51,
            deletion_positions=[53],  # deletion at line 53, outside sym (38-40)
        )
        fd = make_file_diff("model.py", [hunk], [53])
        result = _extract_before_code(sym, fd)
        assert result is None

    def test_multiple_hunks_collects_all(self):
        sym = make_symbol("foo", 5, 25)
        h1 = HunkSnippet(
            before_lines=["old1"], after_lines=["new1"],
            before_start=7, after_start=7,
        )
        h2 = HunkSnippet(
            before_lines=["old2", "old3"], after_lines=["new2"],
            before_start=15, after_start=15,
        )
        fd = make_file_diff("Foo.java", [h1, h2], [7, 15])
        result = _extract_before_code(sym, fd)
        assert result == "old1\nold2\nold3"


class TestMarkChangedSymbols:
    def _make_model_and_diff(self):
        sym1 = make_symbol("processPayment", 10, 25)
        sym2 = make_symbol("validateAmount", 30, 40)
        mod = Module(
            id="PaymentService.java",
            name="PaymentService",
            lang="java",
            summary="Handles payments",
            symbols=[sym1, sym2],
            dependencies=[],
            depth=0,
        )
        meta = MetaModel()
        meta.add(mod)

        hunk = HunkSnippet(
            before_lines=["    old line"],
            after_lines=["    new line"],
            before_start=15, after_start=15,
        )
        fd = make_file_diff("PaymentService.java", [hunk], [15])
        from diffgraph.diff_parser import DiffResult
        dr = DiffResult(
            files={"PaymentService.java": fd},
            changed_files=["PaymentService.java"],
            changed_lines={"PaymentService.java": [15]},
        )
        return meta, dr

    def test_changed_module_id_added(self, tmp_path):
        (tmp_path / "PaymentService.java").write_text(
            "\n" * 9 + "public Order processPayment() {\n" + "    return null;\n" * 15 + "}\n"
        )
        meta, dr = self._make_model_and_diff()
        mark_changed_symbols(meta, dr, str(tmp_path))
        assert "PaymentService.java" in meta.changed_module_ids

    def test_overlapping_symbol_is_marked(self, tmp_path):
        content = "\n" * 40 + "x\n"
        (tmp_path / "PaymentService.java").write_text(content)
        meta, dr = self._make_model_and_diff()
        mark_changed_symbols(meta, dr, str(tmp_path))
        sym = meta.modules["PaymentService.java"].symbols[0]  # lines 10-25, changed=15
        assert sym.is_changed is True

    def test_non_overlapping_symbol_not_marked(self, tmp_path):
        content = "\n" * 40 + "x\n"
        (tmp_path / "PaymentService.java").write_text(content)
        meta, dr = self._make_model_and_diff()
        mark_changed_symbols(meta, dr, str(tmp_path))
        sym = meta.modules["PaymentService.java"].symbols[1]  # lines 30-40
        assert sym.is_changed is False

    def test_changed_symbol_names_populated(self, tmp_path):
        content = "\n" * 40 + "x\n"
        (tmp_path / "PaymentService.java").write_text(content)
        meta, dr = self._make_model_and_diff()
        mark_changed_symbols(meta, dr, str(tmp_path))
        assert "processPayment" in meta.changed_symbol_names
