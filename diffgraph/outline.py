"""
Structural outline of source files using tree-sitter.

get_outline(path, repo_path, changed_lines?) → str

Returns a plain-text outline showing classes, functions/methods, and their
line ranges. Changed symbols (lines overlapping changed_lines) are marked *.
Falls back gracefully when tree-sitter is unavailable or the language is
not supported.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .lang import detect_lang
from .tools import read_file as _read_file

# tree-sitter node types that represent containers (classes, interfaces, etc.)
_CONTAINERS: dict[str, set[str]] = {
    "python": {"class_definition"},
    "java": {
        "class_declaration", "interface_declaration",
        "enum_declaration", "record_declaration",
        "annotation_type_declaration",
    },
    "typescript": {
        "class_declaration", "abstract_class_declaration",
        "interface_declaration",
    },
    "go": {"type_declaration"},
    "kotlin": {"class_declaration", "object_declaration", "interface_declaration"},
    "ruby": {"class", "module"},
    "csharp": {
        "class_declaration", "interface_declaration",
        "struct_declaration", "record_declaration",
        "enum_declaration",
    },
}

# tree-sitter node types that represent members (functions, methods, fields)
_MEMBERS: dict[str, set[str]] = {
    "python": {"function_definition", "decorated_definition"},
    "java": {"method_declaration", "constructor_declaration", "field_declaration"},
    "typescript": {
        "function_declaration", "method_definition",
        "arrow_function", "lexical_declaration",
    },
    "go": {"function_declaration", "method_declaration"},
    "kotlin": {"function_declaration", "property_declaration"},
    "ruby": {"method", "singleton_method"},
    "csharp": {
        "method_declaration", "constructor_declaration",
        "property_declaration", "field_declaration",
    },
}

# Map DiffGraph lang names → tree-sitter-languages names
_TS_LANG: dict[str, str] = {
    "python":     "python",
    "java":       "java",
    "typescript": "typescript",
    "go":         "go",
    "kotlin":     "kotlin",
    "ruby":       "ruby",
    "csharp":     "c_sharp",
}

# tree-sitter language module mapping (for new API: tree-sitter >= 0.23)
_TS_MODULES: dict[str, str] = {
    "python":     "tree_sitter_python",
    "java":       "tree_sitter_java",
    "typescript":  "tree_sitter_typescript",
    "go":         "tree_sitter_go",
    "kotlin":     "tree_sitter_kotlin",
    "ruby":       "tree_sitter_ruby",
    "c_sharp":    "tree_sitter_c_sharp",
}

_parser_cache: dict[str, object] = {}


def _get_parser(lang_name: str):
    """Get a tree-sitter parser via the per-language package
    (tree_sitter_python, tree_sitter_java, …). Requires
    tree-sitter>=0.23 — these are listed in requirements.txt.

    Returns None if the package isn't installed or the language
    isn't in `_TS_MODULES`. No legacy fallback to
    tree_sitter_languages — that aggregator is incompatible with
    tree-sitter>=0.22 (Parser API change) and shipping the dead
    fallback silently produced 'tree-sitter unavailable' on every
    outline call.
    """
    if lang_name in _parser_cache:
        return _parser_cache[lang_name]
    mod_name = _TS_MODULES.get(lang_name)
    if not mod_name:
        return None
    import importlib
    from tree_sitter import Language, Parser
    try:
        mod = importlib.import_module(mod_name)
    except ImportError:
        return None
    # Some packages use language_<name>() instead of language()
    lang_fn = (getattr(mod, "language", None)
               or getattr(mod, f"language_{lang_name}", None))
    if lang_fn is None:
        return None
    parser = Parser(Language(lang_fn()))
    _parser_cache[lang_name] = parser
    return parser


# In-memory session cache (keyed by repo_path/path, no changed_lines variant)
_session_cache: dict[str, str] = {}


@dataclass
class _Sym:
    kind: str       # "class" | "method"
    name: str
    start: int      # 1-indexed
    end: int
    changed: bool = False
    children: list["_Sym"] = field(default_factory=list)


def get_outline(
    path: str,
    repo_path: str,
    changed_lines: Optional[set[int]] = None,
) -> str:
    """
    Return a structural outline of `path`.

    `changed_lines` — 1-indexed line numbers from the diff. Symbols that
    overlap any changed line are marked with *.

    Results without changed_lines are cached for the session lifetime.
    """
    cache_key = f"{repo_path}/{path}"
    if changed_lines is None and cache_key in _session_cache:
        return _session_cache[cache_key]

    content = _read_file(path, repo_path)
    if not content:
        return f"# {path}\n(file not found)\n"

    lang = detect_lang(path)
    ts_lang_name = _TS_LANG.get(lang)
    if ts_lang_name is None:
        total = len(content.splitlines())
        result = f"# {path}  ({total} lines, {lang})\n(structural outline not available for {lang})\n"
        if changed_lines is None:
            _session_cache[cache_key] = result
        return result

    try:
        parser = _get_parser(ts_lang_name)
    except Exception as exc:
        total = len(content.splitlines())
        result = f"# {path}  ({total} lines)\n(tree-sitter unavailable: {exc})\n"
        if changed_lines is None:
            _session_cache[cache_key] = result
        return result
    if parser is None:
        total = len(content.splitlines())
        result = (f"# {path}  ({total} lines)\n"
                   f"(tree-sitter unavailable: no parser for '{ts_lang_name}')\n")
        if changed_lines is None:
            _session_cache[cache_key] = result
        return result

    try:
        tree = parser.parse(content.encode())
    except Exception as exc:
        total = len(content.splitlines())
        result = f"# {path}  ({total} lines)\n(parse error: {exc})\n"
        if changed_lines is None:
            _session_cache[cache_key] = result
        return result

    source_lines = content.splitlines()
    changed = changed_lines or set()
    containers = _CONTAINERS.get(lang, set())
    members = _MEMBERS.get(lang, set())

    symbols = _walk(tree.root_node, source_lines, containers, members, changed)
    result = _format(path, len(source_lines), symbols)

    if changed_lines is None:
        _session_cache[cache_key] = result
    return result


def _walk(
    node,
    lines: list[str],
    containers: set[str],
    members: set[str],
    changed: set[int],
) -> list[_Sym]:
    syms: list[_Sym] = []
    for child in node.children:
        ntype = child.type
        if ntype in containers:
            sym = _make_sym(child, changed, "class")
            sym.children = _walk(child, lines, containers, members, changed)
            syms.append(sym)
        elif ntype in members:
            # decorated_definition in Python wraps either a function or a class;
            # peek inside to choose the correct kind.
            kind = "method"
            if ntype == "decorated_definition":
                for inner in child.children:
                    if inner.type in containers:
                        kind = "class"
                        break
            if kind == "class":
                sym = _make_sym(child, changed, "class")
                sym.children = _walk(child, lines, containers, members, changed)
            else:
                sym = _make_sym(child, changed, "method")
            syms.append(sym)
        else:
            # Recurse into wrapper nodes (block, program body, etc.)
            syms.extend(_walk(child, lines, containers, members, changed))
    return syms


def _make_sym(node, changed: set[int], kind: str) -> _Sym:
    start = node.start_point[0] + 1  # tree-sitter rows are 0-indexed
    end = node.end_point[0] + 1

    # Prefer the named "name" field; fall back to "declarator" or first identifier
    name_node = (
        node.child_by_field_name("name")
        or node.child_by_field_name("declarator")
    )
    if name_node is not None:
        name = name_node.text.decode(errors="replace")
    else:
        name = _first_identifier(node) or f"(anonymous:{start})"

    # A symbol is "changed" if any of its first few lines overlap the changed set
    is_changed = any(ln in changed for ln in range(start, min(end + 1, start + 6)))

    return _Sym(kind=kind, name=name, start=start, end=end, changed=is_changed)


def _first_identifier(node) -> str:
    """Walk children to find the first identifier token."""
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode(errors="replace")
        found = _first_identifier(child)
        if found:
            return found
    return ""


def _format(path: str, total_lines: int, symbols: list[_Sym]) -> str:
    out = [f"# {path}  ({total_lines} lines)"]
    _fmt_syms(symbols, out, indent=0)
    if not symbols:
        out.append("  (no top-level symbols found)")
    return "\n".join(out) + "\n"


def _fmt_syms(symbols: list[_Sym], out: list[str], indent: int) -> None:
    prefix = "  " * indent
    for sym in symbols:
        marker = " *" if sym.changed else ""
        out.append(f"{prefix}[{sym.kind}] {sym.name}  L{sym.start}-{sym.end}{marker}")
        if sym.children:
            _fmt_syms(sym.children, out, indent + 1)
