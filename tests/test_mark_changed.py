"""Tests for mark_changed_symbols."""
import pytest
from diffgraph.diff_parser import FileDiff, HunkSnippet
from diffgraph.diffgraph import mark_changed_symbols
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

    def test_changed_symbol_is_marked(self, tmp_path):
        content = "\n" * 40 + "x\n"
        (tmp_path / "PaymentService.java").write_text(content)
        meta, dr = self._make_model_and_diff()
        mark_changed_symbols(meta, dr, str(tmp_path))
        changed_names = [s.name for s in meta.modules["PaymentService.java"].symbols if s.is_changed]
        assert "processPayment" in changed_names
