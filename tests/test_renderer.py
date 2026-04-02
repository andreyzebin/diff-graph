"""Tests for the renderer."""
import pytest
from diffgraph.model import Dependency, MetaModel, Module, Symbol
from diffgraph.renderer import render, _token_estimate


def make_module(id_, name, depth, symbols=None, summary="does stuff"):
    return Module(
        id=id_, name=name, lang="java", summary=summary,
        symbols=symbols or [], dependencies=[], depth=depth,
    )


def make_changed_symbol(name, before=None, after=None):
    sym = Symbol(
        name=name, kind="METHOD", signature=f"void {name}()",
        summary="does something", annotations=["@Transactional"],
        start_line=10, end_line=20, is_changed=True,
        full_code=after, before_code=before,
    )
    return sym


class TestBasicRender:
    def _build_meta(self):
        sym = make_changed_symbol(
            "processPayment",
            before="old code",
            after="new code",
        )
        changed_mod = make_module("PaymentService.java", "PaymentService", 0, [sym])
        dep_mod = make_module("CardValidator.java", "CardValidator", 1)
        trans_mod = make_module("LuhnAlgorithm.java", "LuhnAlgorithm", 2)

        meta = MetaModel()
        meta.add(changed_mod)
        meta.add(dep_mod)
        meta.add(trans_mod)
        meta.changed_module_ids = ["PaymentService.java"]
        meta.changed_symbol_names = ["processPayment"]
        return meta

    def test_changed_modules_section_present(self):
        meta = self._build_meta()
        out = render(meta)
        assert "## Changed Files" in out

    def test_changed_module_filename_present(self):
        meta = self._build_meta()
        out = render(meta)
        assert "PaymentService.java" in out

    def test_changed_symbol_signature_present(self):
        # fallback (no repo_path): signature appears in pseudocode block
        meta = self._build_meta()
        out = render(meta)
        assert "processPayment" in out

    def test_changed_symbol_shows_full_code(self):
        # when is_changed=True, full_code appears in fallback renderer
        sym = make_changed_symbol("processPayment", before="old code", after="new code")
        mod = make_module("PaymentService.java", "PaymentService", 0, [sym])
        meta = MetaModel()
        meta.add(mod)
        meta.changed_module_ids = ["PaymentService.java"]
        out = render(meta)
        assert "new code" in out

    def test_direct_deps_section(self):
        meta = self._build_meta()
        out = render(meta)
        assert "## Direct Dependencies" in out
        assert "CardValidator.java" in out

    def test_transitive_deps_section(self):
        meta = self._build_meta()
        out = render(meta)
        assert "## Transitive Dependencies" in out
        assert "LuhnAlgorithm" in out

    def test_annotation_present(self):
        meta = self._build_meta()
        out = render(meta)
        assert "@Transactional" in out

    def test_dep_usage_annotation(self):
        # dep with usage_summary shows in direct deps section
        sym = make_changed_symbol("processPayment")
        changed_mod = Module(
            id="PaymentService.java", name="PaymentService", lang="java",
            summary="handles payments", depth=0, symbols=[sym],
            dependencies=[
                Dependency(
                    name="CardValidator", fqn="com.example.CardValidator",
                    usage="validates card numbers",
                    file_path="CardValidator.java",
                    usage_summary="validates credit card numbers before charging",
                )
            ],
        )
        dep_mod = make_module("CardValidator.java", "CardValidator", 1)
        meta = MetaModel()
        meta.add(changed_mod)
        meta.add(dep_mod)
        meta.changed_module_ids = ["PaymentService.java"]
        out = render(meta)
        assert "validates credit card numbers before charging" in out


class TestTokenBudget:
    def test_token_estimate(self):
        assert _token_estimate("a" * 400) == 100

    def test_exceeds_budget_degrades_depth2(self):
        sym = make_changed_symbol("foo", before="x", after="y")
        changed = make_module("Changed.java", "Changed", 0, [sym], summary="main")
        trans = make_module("Trans.java", "Trans", 2, summary="transitive detail " * 20)

        meta = MetaModel()
        meta.add(changed)
        meta.add(trans)
        meta.changed_module_ids = ["Changed.java"]

        full = render(meta, max_tokens=99999)
        small = render(meta, max_tokens=10)

        # Full render: depth-2 shows id + summary
        assert "Trans.java" in full
        assert "transitive detail" in full
        # Degraded render: depth-2 shows name only (short form)
        assert "Trans.java" not in small
        assert "Trans" in small

    def test_changed_module_never_truncated(self):
        sym = make_changed_symbol("critical", before="old", after="new")
        changed = make_module("Core.java", "Core", 0, [sym])
        meta = MetaModel()
        meta.add(changed)
        meta.changed_module_ids = ["Core.java"]

        out = render(meta, max_tokens=1)  # absurdly small
        assert "Core.java" in out
        assert "critical" in out
