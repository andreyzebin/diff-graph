"""
Two-pass outline for virtual unified diff files.

Parses the virtual file twice (blanking +/- lines alternately) to get
valid code for tree-sitter. Merges results into a single outline with
Lold/Lnew ranges for changed methods.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Symbol:
    kind: str       # "class", "method", "field"
    name: str
    start: int      # L (unified position, 1-indexed)
    end: int        # L
    children: list["Symbol"] = field(default_factory=list)


@dataclass
class MergedSymbol:
    kind: str
    name: str
    l_old_start: int | None = None
    l_old_end: int | None = None
    l_new_start: int | None = None
    l_new_end: int | None = None
    old_start: int | None = None  # real line in base commit
    old_end: int | None = None
    new_start: int | None = None  # real line in source commit
    new_end: int | None = None
    changed: bool = False
    children: list["MergedSymbol"] = field(default_factory=list)


def parse_outline_vfs(
    vfs_dir: str,
    path: str,
    meta: list[dict] | None = None,
) -> str:
    """
    Structural outline for a file in the virtual FS.

    For changed files: two-pass parsing, merged output with Lold/Lnew.
    For unchanged files: single-pass, plain L numbers.
    """
    file_path = Path(vfs_dir) / path
    if not file_path.exists():
        return f"(file not found: {path})"

    lines = file_path.read_text().splitlines()

    if not meta:
        # Unchanged file — parse directly
        symbols = _parse_symbols(lines, path)
        if symbols is None:
            return f"# {path}  ({len(lines)} lines)\n(tree-sitter unavailable)"
        return _format_unchanged(path, len(lines), symbols)

    # Changed file — two-pass parsing
    changed_Ls = {m["L"] for m in meta if m["marker"] != " "}

    # New-side: blank "-" lines
    new_lines = [
        "" if meta[i]["marker"] == "-" else lines[i]
        for i in range(len(lines))
    ]
    new_symbols = _parse_symbols(new_lines, path)

    # Old-side: blank "+" lines
    old_lines = [
        "" if meta[i]["marker"] == "+" else lines[i]
        for i in range(len(lines))
    ]
    old_symbols = _parse_symbols(old_lines, path)

    if new_symbols is None and old_symbols is None:
        return f"# {path}  ({len(lines)} lines)\n(tree-sitter unavailable)"

    # Merge
    merged = _merge_symbols(
        old_symbols or [], new_symbols or [],
        meta, changed_Ls,
    )

    return _format_merged(path, len(lines), merged)


def _parse_symbols(lines: list[str], path: str) -> list[Symbol] | None:
    """Parse symbols from lines using tree-sitter. Returns None if unavailable.

    Delegates to diffgraph.outline._get_parser, which prefers the
    modern per-language packages (tree_sitter_java, _python, …)
    and only falls back to the legacy tree_sitter_languages
    aggregator when those aren't present. The legacy package is
    incompatible with tree-sitter>=0.22 (Parser API change), so
    relying on it alone breaks diff_outline on fresh installs.
    """
    try:
        from diffgraph.lang import detect_lang
        from diffgraph.outline import _CONTAINERS, _MEMBERS, _TS_LANG, _get_parser
    except ImportError:
        return None

    lang = detect_lang(path)
    ts_lang_name = _TS_LANG.get(lang)
    if not ts_lang_name:
        return None

    try:
        parser = _get_parser(ts_lang_name)
    except Exception:
        return None
    if parser is None:
        return None

    content = "\n".join(lines)
    try:
        tree = parser.parse(content.encode())
    except Exception:
        return None

    containers = _CONTAINERS.get(lang, set())
    members = _MEMBERS.get(lang, set())

    return _walk(tree.root_node, containers, members)


def _walk(node, containers: set[str], members: set[str]) -> list[Symbol]:
    """Walk AST and extract symbols with L positions."""
    syms: list[Symbol] = []
    for child in node.children:
        ntype = child.type
        if ntype in containers:
            sym = _make_sym(child, "class")
            sym.children = _walk(child, containers, members)
            syms.append(sym)
        elif ntype in members:
            kind = "method"
            if ntype == "decorated_definition":
                for inner in child.children:
                    if inner.type in containers:
                        kind = "class"
                        break
            if ntype == "field_declaration":
                kind = "field"
            if kind == "class":
                sym = _make_sym(child, "class")
                sym.children = _walk(child, containers, members)
            else:
                sym = _make_sym(child, kind)
            syms.append(sym)
        else:
            syms.extend(_walk(child, containers, members))
    return syms


def _make_sym(node, kind: str) -> Symbol:
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1
    name_node = (
        node.child_by_field_name("name")
        or node.child_by_field_name("declarator")
    )
    if name_node is not None:
        name = name_node.text.decode(errors="replace")
    else:
        name = _first_id(node) or f"(anon:{start})"
    return Symbol(kind=kind, name=name, start=start, end=end)


def _first_id(node) -> str:
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode(errors="replace")
        found = _first_id(child)
        if found:
            return found
    return ""


def _L_to(meta: list[dict], L: int, which: str) -> int | None:
    if 1 <= L <= len(meta):
        return meta[L - 1].get(which)
    return None


def _merge_symbols(
    old_syms: list[Symbol],
    new_syms: list[Symbol],
    meta: list[dict],
    changed_Ls: set[int],
) -> list[MergedSymbol]:
    """Merge old-side and new-side symbols by name."""
    old_by_name: dict[str, Symbol] = {s.name: s for s in old_syms}
    new_by_name: dict[str, Symbol] = {s.name: s for s in new_syms}
    all_names = list(dict.fromkeys(
        [s.name for s in old_syms] + [s.name for s in new_syms]
    ))

    merged: list[MergedSymbol] = []
    for name in all_names:
        o = old_by_name.get(name)
        n = new_by_name.get(name)

        ms = MergedSymbol(
            kind=(n or o).kind,
            name=name,
        )

        if o:
            ms.l_old_start = o.start
            ms.l_old_end = o.end
            ms.old_start = _L_to(meta, o.start, "old")
            ms.old_end = _L_to(meta, o.end, "old")

        if n:
            ms.l_new_start = n.start
            ms.l_new_end = n.end
            ms.new_start = _L_to(meta, n.start, "new")
            ms.new_end = _L_to(meta, n.end, "new")

        # Determine full L range
        starts = [x for x in [ms.l_old_start, ms.l_new_start] if x is not None]
        ends = [x for x in [ms.l_old_end, ms.l_new_end] if x is not None]
        if starts and ends:
            full_start = min(starts)
            full_end = max(ends)
            ms.changed = any(L in changed_Ls for L in range(full_start, full_end + 1))

        # Merge children recursively
        if (o and o.children) or (n and n.children):
            ms.children = _merge_symbols(
                o.children if o else [],
                n.children if n else [],
                meta, changed_Ls,
            )

        merged.append(ms)

    return merged


def _format_unchanged(path: str, total: int, symbols: list[Symbol]) -> str:
    out = [f"# {path}  ({total} lines)"]
    _fmt_syms(symbols, out, 0)
    if not symbols:
        out.append("  (no symbols found)")
    return "\n".join(out)


def _fmt_syms(symbols: list[Symbol], out: list[str], indent: int) -> None:
    prefix = "  " * indent
    for s in symbols:
        out.append(f"{prefix}[{s.kind}] {s.name}  L{s.start}-{s.end}")
        if s.children:
            _fmt_syms(s.children, out, indent + 1)


def _format_merged(path: str, total: int, symbols: list[MergedSymbol]) -> str:
    out = [f"# {path}  ({total} lines)"]
    _fmt_merged(symbols, out, 0)
    if not symbols:
        out.append("  (no symbols found)")
    return "\n".join(out)


def _fmt_merged(symbols: list[MergedSymbol], out: list[str], indent: int) -> None:
    prefix = "  " * indent
    for s in symbols:
        star = " *" if s.changed else ""

        has_old = s.l_old_start is not None
        has_new = s.l_new_start is not None
        is_same_L = (has_old and has_new
                     and s.l_old_start == s.l_new_start
                     and s.l_old_end == s.l_new_end)

        # L range part
        if has_old and has_new and not is_same_L:
            # Changed method — show Lold and Lnew
            l_part = f"Lold:{s.l_old_start}-{s.l_old_end} Lnew:{s.l_new_start}-{s.l_new_end}"
        elif has_old and not has_new:
            # Deleted
            l_part = f"L{s.l_old_start}-{s.l_old_end}"
        elif has_new and not has_old:
            # Added
            l_part = f"L{s.l_new_start}-{s.l_new_end}"
        else:
            # Unchanged or same L
            start = s.l_new_start or s.l_old_start
            end = s.l_new_end or s.l_old_end
            l_part = f"L{start}-{end}"

        # old/new real line numbers
        old_str = f"old:{s.old_start}-{s.old_end}" if s.old_start and s.old_end else ""
        new_str = f"new:{s.new_start}-{s.new_end}" if s.new_start and s.new_end else ""

        if not old_str and new_str:
            mapping = f"(added → {new_str})"
        elif old_str and not new_str:
            mapping = f"({old_str} → deleted)"
        elif old_str and new_str:
            mapping = f"({old_str} → {new_str})"
        else:
            mapping = ""

        out.append(f"{prefix}[{s.kind}] {s.name}  {l_part} {mapping}{star}")

        if s.children:
            _fmt_merged(s.children, out, indent + 1)
