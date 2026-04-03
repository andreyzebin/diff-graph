from __future__ import annotations
from typing import Optional

from .agents.prompts import load as _load_prompt
from .diff_parser import DiffResult
from .model import MetaModel, Module, Symbol
from .tools import read_file


# ── load section templates from prompt file ───────────────────────────────────

def _parse_sections(text: str) -> dict[str, str]:
    """Parse a template file with SECTION:name markers into a name→template dict."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("SECTION:"):
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current = line[8:].strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return sections


_S = _parse_sections(_load_prompt("render_context.txt"))

_COMMENT_CHAR: dict[str, str] = {
    "python": "#", "ruby": "#",
    "java": "//", "go": "//", "typescript": "//", "kotlin": "//", "csharp": "//",
}

_OMIT: dict[str, str] = {
    "python":     "...  # [omitted]",
    "java":       "// [omitted]",
    "go":         "// [omitted]",
    "typescript": "// [omitted]",
    "kotlin":     "// [omitted]",
    "ruby":       "# [omitted]",
    "csharp":     "// [omitted]",
}


def _compressed_header(lang: str) -> str:
    c = _COMMENT_CHAR.get(lang, "//")
    return _S["compressed_note"].format(comment_char=c)


def _section(tmpl: str, content: str) -> str:
    """Return section block if content is non-empty, else empty string."""
    if not content.strip():
        return ""
    return tmpl.format(content=content) + "\n"


# ── public API ────────────────────────────────────────────────────────────────

def render(
    model: MetaModel,
    diff_result: Optional[DiffResult] = None,
    repo_path: str = "",
    max_tokens: int = 8000,
    pr_title: str = "",
    pr_description: str = "",
) -> str:
    """
    Render a MetaModel as a text prompt context.

    Structure (defined in render_context.txt):
      1. PR title + description (if provided)
      2. Raw diff
      3. Full after-version of each changed file
      4. Callers — compressed with expanded symbols shown in full
      5. Direct dependencies — compressed
      6. Transitive dependencies — one-line summaries

    Token budget: if over limit, degrade depth-2 → names only,
    then depth-1 → summaries only. Changed files and callers are never cut.
    """
    def _wrap(body: str) -> str:
        if not pr_title:
            return body
        return _S["pr_header"].format(
            pr_title=pr_title,
            pr_description=pr_description.strip() or "_No description provided._",
        ) + "\n" + body

    full = _build(model, diff_result, repo_path, trunc2=False, trunc1=False)
    if _tok(_wrap(full)) <= max_tokens:
        return _wrap(full)

    degraded = _build(model, diff_result, repo_path, trunc2=True, trunc1=False)
    if _tok(_wrap(degraded)) <= max_tokens:
        return _wrap(degraded)

    return _wrap(_build(model, diff_result, repo_path, trunc2=True, trunc1=True))


# ── build ─────────────────────────────────────────────────────────────────────

def _build(
    model: MetaModel,
    diff_result: Optional[DiffResult],
    repo_path: str,
    trunc2: bool,
    trunc1: bool,
) -> str:
    parts: list[str] = []
    depths = model.compute_depths()

    # 1. Raw diff
    if diff_result and diff_result.raw_text.strip():
        parts.append(_section(_S["diff"], diff_result.raw_text.rstrip()))

    # 2. Changed files — full content from disk
    changed_mods = [model.modules[mid] for mid in model.changed_module_ids if mid in model.modules]
    if changed_mods:
        files_parts: list[str] = []
        for mod in changed_mods:
            status = _file_status(mod.id, diff_result)
            header = _S["file_header"].format(file_id=mod.id, status=status.upper())
            content = read_file(mod.id, repo_path) if repo_path else ""
            if content:
                body = f"```{mod.lang}\n{content.rstrip()}\n```"
            else:
                body = _render_fallback(mod)
            files_parts.append(f"{header}\n{body}\n")
        parts.append(_section(_S["changed_files"], "\n".join(files_parts)))

    # 3. Callers / impact
    callers = [mod for mid, mod in model.modules.items() if depths.get(mid) == -1]
    if callers:
        caller_parts: list[str] = []
        for mod in callers:
            header = _S["caller_header"].format(file_id=mod.id)
            compressed = _render_compressed(mod, repo_path)
            if compressed:
                body = f"```{mod.lang}\n{compressed}\n```"
                caller_parts.append(f"{header}\n\n{body}\n")
        if caller_parts:
            parts.append(_section(_S["callers"], "\n".join(caller_parts)))

    # 4. Direct dependencies (depth 1)
    dep_usage = _build_dep_usage_index(model)
    depth1 = [mod for mid, mod in model.modules.items() if depths.get(mid) == 1]
    if depth1:
        dep_parts: list[str] = []
        for mod in depth1:
            header = _S["dep_header"].format(file_id=mod.id)
            usage = dep_usage.get(mod.id, "")
            usage_line = ("\n" + _S["dep_usage"].format(usage=usage)) if usage else ""
            if trunc1:
                body = f'> "{mod.summary}"'
            else:
                compressed = _render_compressed(mod, repo_path)
                body = f"```{mod.lang}\n{compressed}\n```" if compressed else f'> "{mod.summary}"'
            dep_parts.append(f"{header}{usage_line}\n\n{body}\n")
        parts.append(_section(_S["direct_deps"], "\n".join(dep_parts)))

    # 5. Transitive dependencies (depth 2+)
    depth2 = [mod for mid, mod in model.modules.items() if depths.get(mid, 0) >= 2]
    if depth2:
        tmpl = _S["transitive_item_short"] if trunc2 else _S["transitive_item_full"]
        lines = [tmpl.format(file_id=mod.id, name=mod.name, summary=mod.summary)
                 for mod in depth2]
        parts.append(_section(_S["transitive_deps"], "\n".join(lines)))

    return "\n".join(parts)


# ── file compression ──────────────────────────────────────────────────────────

def _render_compressed(mod: Module, repo_path: str) -> str:
    """
    Render a compressed but readable view of a source file.

    Algorithm:
      1. Read the full file from disk once.
      2. For each TOP-LEVEL symbol:
           - expanded/changed → keep all lines unchanged
           - has nested expanded symbols → partial compression: compress only the
             unexpanded inner symbols, leave expanded ones fully visible
           - otherwise → keep signature line only; replace body with omit marker
      3. All other lines (imports, module-level code, blank lines) are kept as-is.
    """
    if not repo_path:
        return _render_fallback(mod)

    full = read_file(mod.id, repo_path)
    if not full:
        return _render_fallback(mod)

    lines = full.splitlines()
    omit_token = _OMIT.get(mod.lang, "// ...")
    sorted_syms = sorted(mod.symbols, key=lambda s: s.start_line)
    top = _top_level_symbols(sorted_syms)

    if not top:
        return full

    replacements: dict[int, str | None] = {}

    for sym in top:
        if sym.is_expanded or sym.is_changed:
            continue

        sig_0 = sym.start_line - 1
        end_0 = sym.end_line - 1
        if end_0 <= sig_0:
            continue

        nested_expanded = [
            s for s in sorted_syms
            if s is not sym
            and s.start_line > sym.start_line
            and s.end_line <= sym.end_line
            and (s.is_expanded or s.is_changed)
        ]

        if not nested_expanded:
            body_0 = _body_start(lines, sig_0, end_0)
            indent = _body_indent(lines, body_0 - 1, end_0)
            replacements[body_0] = " " * indent + omit_token
            for i in range(body_0 + 1, end_0 + 1):
                replacements[i] = None
        else:
            nested_syms = [
                s for s in sorted_syms
                if s is not sym
                and s.start_line > sym.start_line
                and s.end_line <= sym.end_line
            ]
            for nested in _top_level_symbols(nested_syms):
                if nested.is_expanded or nested.is_changed:
                    continue
                n_sig_0 = nested.start_line - 1
                n_end_0 = nested.end_line - 1
                if n_end_0 > n_sig_0:
                    n_body_0 = _body_start(lines, n_sig_0, n_end_0)
                    n_indent = _body_indent(lines, n_body_0 - 1, n_end_0)
                    replacements[n_body_0] = " " * n_indent + omit_token
                    for i in range(n_body_0 + 1, n_end_0 + 1):
                        replacements[i] = None

    header = _compressed_header(mod.lang) if replacements else ""
    out: list[str] = [header] if header else []
    for i, line in enumerate(lines):
        if i not in replacements:
            out.append(line)
        elif replacements[i] is not None:
            out.append(replacements[i])

    return "\n".join(out)


def _body_start(lines: list[str], sig_0: int, end_0: int) -> int:
    for i in range(sig_0, end_0):
        stripped = lines[i].rstrip()
        if stripped.endswith(":") or stripped.endswith("{"):
            return i + 1
    return sig_0 + 1


def _body_indent(lines: list[str], sig_0: int, end_0: int) -> int:
    for i in range(sig_0 + 1, end_0 + 1):
        stripped = lines[i].lstrip()
        if stripped:
            return len(lines[i]) - len(stripped)
    return 4


def _top_level_symbols(symbols: list[Symbol]) -> list[Symbol]:
    result: list[Symbol] = []
    max_end = 0
    for sym in symbols:
        if sym.start_line > max_end:
            result.append(sym)
            max_end = sym.end_line
    return result


def _render_fallback(mod: Module) -> str:
    omit_token = _OMIT.get(mod.lang, "// ...")
    lines: list[str] = [f'# {mod.summary}', ""]
    for sym in mod.symbols:
        ann = (" " + " ".join(sym.annotations)) if sym.annotations else ""
        lines.append(f"{sym.signature}{ann}")
        if sym.is_changed and sym.full_code:
            for cl in sym.full_code.splitlines():
                lines.append(f"    {cl}")
        else:
            lines.append(f"    {omit_token}")
        lines.append("")
    return "\n".join(lines)


def _build_dep_usage_index(model: MetaModel) -> dict[str, str]:
    index: dict[str, str] = {}
    for mid in model.changed_module_ids:
        mod = model.modules.get(mid)
        if not mod:
            continue
        for dep in mod.dependencies:
            if dep.file_path:
                index[dep.file_path] = dep.usage
    return index


def _file_status(path: str, diff_result: Optional[DiffResult]) -> str:
    if diff_result is None:
        return "MODIFIED"
    fd = diff_result.files.get(path)
    return fd.status.upper() if fd else "MODIFIED"


def _token_estimate(text: str) -> int:
    return len(text) // 4


_tok = _token_estimate
