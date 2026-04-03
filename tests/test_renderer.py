"""Tests for the renderer."""
import pytest
from diffgraph.model import Dependency, MetaModel, Module, Symbol
from diffgraph.renderer import render, _render_compressed, _token_estimate


def make_module(id_, name, symbols=None, summary="does stuff"):
    return Module(
        id=id_, name=name, lang="java", summary=summary,
        symbols=symbols or [], dependencies=[],
    )


def make_dep(name, file_path):
    return Dependency(name=name, fqn=f"com.example.{name}", usage="used", file_path=file_path)


def make_changed_symbol(name, after=None):
    sym = Symbol(
        name=name, kind="METHOD", signature=f"void {name}()",
        summary="does something", annotations=["@Transactional"],
        start_line=10, end_line=20, is_changed=True,
        full_code=after,
    )
    return sym


class TestBasicRender:
    def _build_meta(self):
        sym = make_changed_symbol("processPayment", after="new code")
        changed_mod = make_module("PaymentService.java", "PaymentService", [sym])
        dep_mod = make_module("CardValidator.java", "CardValidator")
        trans_mod = make_module("LuhnAlgorithm.java", "LuhnAlgorithm")

        # Wire dependency edges so compute_depths() works
        changed_mod.dependencies = [make_dep("CardValidator", "CardValidator.java")]
        dep_mod.dependencies = [make_dep("LuhnAlgorithm", "LuhnAlgorithm.java")]

        meta = MetaModel()
        meta.add(changed_mod)
        meta.add(dep_mod)
        meta.add(trans_mod)
        meta.changed_module_ids = ["PaymentService.java"]
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
        sym = make_changed_symbol("processPayment", after="new code")
        mod = make_module("PaymentService.java", "PaymentService", [sym])
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
        # dep usage shows in direct deps section
        sym = make_changed_symbol("processPayment")
        changed_mod = Module(
            id="PaymentService.java", name="PaymentService", lang="java",
            summary="handles payments", symbols=[sym],
            dependencies=[
                Dependency(
                    name="CardValidator", fqn="com.example.CardValidator",
                    usage="validates credit card numbers before charging",
                    file_path="CardValidator.java",
                )
            ],
        )
        dep_mod = make_module("CardValidator.java", "CardValidator")
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
        sym = make_changed_symbol("foo", after="y")
        changed = make_module("Changed.java", "Changed", [sym], summary="main")
        direct = make_module("Direct.java", "Direct", summary="direct dep")
        trans = make_module("Trans.java", "Trans", summary="transitive detail " * 20)

        # Changed → Direct → Trans (depth 2)
        changed.dependencies = [make_dep("Direct", "Direct.java")]
        direct.dependencies = [make_dep("Trans", "Trans.java")]

        meta = MetaModel()
        meta.add(changed)
        meta.add(direct)
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
        sym = make_changed_symbol("critical", after="new")
        changed = make_module("Core.java", "Core", [sym])
        meta = MetaModel()
        meta.add(changed)
        meta.changed_module_ids = ["Core.java"]

        out = render(meta, max_tokens=1)  # absurdly small
        assert "Core.java" in out
        assert "critical" in out


class TestPartialCompression:
    """_render_compressed should expand only selected nested symbols, compress the rest."""

    JAVA_SOURCE = (
        "public class OrderController {\n"          # line 1
        "    private OrderService svc;\n"            # line 2
        "    public void createOrder() {\n"          # line 3
        "        svc.create();\n"                    # line 4
        "    }\n"                                    # line 5
        "    public void cancelOrder() {\n"          # line 6
        "        svc.cancel();\n"                    # line 7
        "    }\n"                                    # line 8
        "    public void getOrder() {\n"             # line 9
        "        svc.get();\n"                       # line 10
        "    }\n"                                    # line 11
        "}\n"                                        # line 12
    )

    def _make_mod(self, tmp_path):
        src = tmp_path / "OrderController.java"
        src.write_text(self.JAVA_SOURCE)
        repo = str(tmp_path)

        cls_sym = Symbol(
            name="OrderController", kind="CLASS",
            signature="public class OrderController",
            summary="", annotations=[], start_line=1, end_line=12,
        )
        create_sym = Symbol(
            name="createOrder", kind="METHOD",
            signature="public void createOrder()",
            summary="", annotations=[], start_line=3, end_line=5,
        )
        cancel_sym = Symbol(
            name="cancelOrder", kind="METHOD",
            signature="public void cancelOrder()",
            summary="", annotations=[], start_line=6, end_line=8,
        )
        get_sym = Symbol(
            name="getOrder", kind="METHOD",
            signature="public void getOrder()",
            summary="", annotations=[], start_line=9, end_line=11,
        )
        mod = Module(
            id="OrderController.java", name="OrderController", lang="java",
            summary="controller", symbols=[cls_sym, create_sym, cancel_sym, get_sym],
            dependencies=[],
        )
        return mod, repo

    def test_no_expansion_compresses_whole_class(self, tmp_path):
        mod, repo = self._make_mod(tmp_path)
        out = _render_compressed(mod, repo)
        assert "svc.create()" not in out
        assert "svc.cancel()" not in out
        assert "svc.get()" not in out
        assert "// [omitted]" in out

    def test_expanded_method_shown_others_compressed(self, tmp_path):
        mod, repo = self._make_mod(tmp_path)
        # Mark only cancelOrder as expanded
        for sym in mod.symbols:
            if sym.name == "cancelOrder":
                sym.is_expanded = True
        out = _render_compressed(mod, repo)
        # cancelOrder body visible
        assert "svc.cancel()" in out
        # other methods compressed
        assert "svc.create()" not in out
        assert "svc.get()" not in out
