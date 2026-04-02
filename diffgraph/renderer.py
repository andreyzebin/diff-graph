from __future__ import annotations
from typing import Optional

from .diff_parser import DiffResult
from .model import MetaModel, Module, Symbol
from .tools import read_file

# Language-specific body omission marker (used inline in compressed files)
_OMIT: dict[str, str] = {
    "python":     "...  # [omitted — not on call trace]",
    "java":       "// [omitted — not on call trace]",
    "go":         "// [omitted — not on call trace]",
    "typescript": "// [omitted — not on call trace]",
    "kotlin":     "// [omitted — not on call trace]",
    "ruby":       "# [omitted — not on call trace]",
    "csharp":     "// [omitted — not on call trace]",
}

_COMMENT_CHAR: dict[str, str] = {
    "python": "#", "ruby": "#",
    "java": "//", "go": "//", "typescript": "//", "kotlin": "//", "csharp": "//",
}


def _compressed_header(lang: str) -> str:
    c = _COMMENT_CHAR.get(lang, "//")
    return (
        f"{c} NOTE: This file is shown in compressed form for code review context.\n"
        f"{c} Symbols on the call trace are shown in full.\n"
        f"{c} All other symbol bodies are replaced with an [omitted] marker.\n"
    )


def render(
    model: MetaModel,
    diff_result: Optional[DiffResult] = None,
    repo_path: str = "",
    max_tokens: int = 8000,
) -> str:
    """
    Render a MetaModel as a text prompt context.

    Structure:
      1. Raw diff
      2. Full after-version of each changed file (read from disk)
      3. Callers — source file with trace-aware compression
      4. Direct dependencies (depth 1) — source file with trace-aware compression
      5. Transitive dependencies (depth 2+) — one-line summaries

    Compression: symbols NOT on the call trace keep only their signature line;
    their body is replaced by a language-appropriate '...' marker so the LLM
    understands the omission is intentional.

    Token budget: if over limit, degrade depth-2 → names only,
    then depth-1 → summaries only. Changed files and callers are never cut.
    """
    full = _build(model, diff_result, repo_path, trunc2=False, trunc1=False)
    if _tok(full) <= max_tokens:
        return full

    degraded = _build(model, diff_result, repo_path, trunc2=True, trunc1=False)
    if _tok(degraded) <= max_tokens:
        return degraded

    return _build(model, diff_result, repo_path, trunc2=True, trunc1=True)


# ── build ─────────────────────────────────────────────────────────────────────

def _build(
    model: MetaModel,
    diff_result: Optional[DiffResult],
    repo_path: str,
    trunc2: bool,
    trunc1: bool,
) -> str:
    parts: list[str] = []
    changed_ids = set(model.changed_module_ids)

    # 1. Raw diff
    if diff_result and diff_result.raw_text.strip():
        parts.append("## Diff\n")
        parts.append("```diff")
        parts.append(diff_result.raw_text.rstrip())
        parts.append("```\n")

    # 2. Changed files — full content from disk
    changed_mods = [model.modules[mid] for mid in model.changed_module_ids if mid in model.modules]
    if changed_mods:
        parts.append("## Changed Files\n")
        for mod in changed_mods:
            status = _file_status(mod.id, diff_result)
            parts.append(f"### {mod.id} [{status}]\n")
            content = read_file(mod.id, repo_path) if repo_path else ""
            if content:
                parts.append(f"```{mod.lang}")
                parts.append(content.rstrip())
                parts.append("```\n")
            else:
                parts.append(_render_fallback(mod))

    # 3. Callers / impact
    callers = [model.modules[mid] for mid in model.caller_module_ids if mid in model.modules]
    if callers:
        parts.append("## Callers — Impact Analysis\n")
        for mod in callers:
            reason = model.caller_reasons.get(mod.id, "")
            parts.append(f"### {mod.id}")
            if reason:
                parts.append(f"> {reason}")
            parts.append("")
            compressed = _render_compressed(mod, repo_path)
            if compressed:
                parts.append(f"```{mod.lang}")
                parts.append(compressed)
                parts.append("```\n")

    # 4. Direct dependencies (depth 1)
    depth1 = [m for m in model.modules.values() if m.depth == 1 and m.id not in changed_ids]
    if depth1:
        parts.append("## Direct Dependencies\n")
        for mod in depth1:
            parts.append(f"### {mod.id}\n")
            if trunc1:
                parts.append(f'> "{mod.summary}"\n')
            else:
                compressed = _render_compressed(mod, repo_path)
                if compressed:
                    parts.append(f"```{mod.lang}")
                    parts.append(compressed)
                    parts.append("```\n")

    # 5. Transitive dependencies (depth 2+)
    depth2 = [m for m in model.modules.values() if m.depth >= 2 and m.id not in changed_ids]
    if depth2:
        parts.append("## Transitive Dependencies\n")
        for mod in depth2:
            if trunc2:
                parts.append(f"- {mod.name}")
            else:
                parts.append(f'- {mod.id} — "{mod.summary}"')

    return "\n".join(parts)


# ── file compression ──────────────────────────────────────────────────────────

def _render_compressed(mod: Module, repo_path: str) -> str:
    """
    Render a compressed but readable view of a source file.

    Algorithm:
      1. Read the full file from disk once.
      2. For each TOP-LEVEL symbol (nested symbols are skipped — they're part of
         their parent):
           - on-trace  → keep all lines unchanged
           - off-trace → keep signature line; if the symbol has a body
                         (end_line > start_line) replace first body line with the
                         language-appropriate omit marker and suppress the rest
      3. All other lines (imports, module-level code, blank lines) are kept as-is.
    """
    if not repo_path:
        return _render_fallback(mod)

    full = read_file(mod.id, repo_path)
    if not full:
        return _render_fallback(mod)

    lines = full.splitlines()
    omit_token = _OMIT.get(mod.lang, "// ...")
    top = _top_level_symbols(sorted(mod.symbols, key=lambda s: s.start_line))

    if not top:
        return full

    # replacements[i] = replacement string, or None to suppress the line entirely
    replacements: dict[int, str | None] = {}

    for sym in top:
        if sym.is_on_trace:
            continue
        sig_0 = sym.start_line - 1  # 0-based
        end_0 = sym.end_line - 1    # 0-based
        # Only compress symbols that actually have a body
        if end_0 > sig_0:
            body_0 = _body_start(lines, sig_0, end_0)
            indent = _body_indent(lines, body_0 - 1, end_0)
            replacements[body_0] = " " * indent + omit_token
            for i in range(body_0 + 1, end_0 + 1):
                replacements[i] = None

    out: list[str] = [_compressed_header(mod.lang)]
    for i, line in enumerate(lines):
        if i not in replacements:
            out.append(line)
        elif replacements[i] is not None:
            out.append(replacements[i])
        # else None → suppressed

    return "\n".join(out)


def _body_start(lines: list[str], sig_0: int, end_0: int) -> int:
    """Return 0-based index of the first body line.

    Scans forward from sig_0 for the line that closes the signature
    (ends with ':' for Python, or '{' for C-family languages), then
    returns the next line.  Falls back to sig_0 + 1 if not found.
    """
    for i in range(sig_0, end_0):
        stripped = lines[i].rstrip()
        if stripped.endswith(":") or stripped.endswith("{"):
            return i + 1
    return sig_0 + 1


def _body_indent(lines: list[str], sig_0: int, end_0: int) -> int:
    """Return the indentation of the first non-blank body line."""
    for i in range(sig_0 + 1, end_0 + 1):
        stripped = lines[i].lstrip()
        if stripped:
            return len(lines[i]) - len(stripped)
    return 4


def _top_level_symbols(symbols: list[Symbol]) -> list[Symbol]:
    """Return only symbols not nested inside another (determined by line ranges)."""
    result: list[Symbol] = []
    max_end = 0
    for sym in symbols:
        if sym.start_line > max_end:
            result.append(sym)
            max_end = sym.end_line
    return result


def _render_fallback(mod: Module) -> str:
    """No repo_path available: render as signature-only pseudocode."""
    omit_token = _OMIT.get(mod.lang, "// ...")
    lines: list[str] = [f'# {mod.summary}', ""]
    for sym in mod.symbols:
        ann = (" " + " ".join(sym.annotations)) if sym.annotations else ""
        lines.append(f"{sym.signature}{ann}")
        if sym.is_on_trace and sym.full_code:
            for cl in sym.full_code.splitlines():
                lines.append(f"    {cl}")
        else:
            lines.append(f"    {omit_token}")
        lines.append("")
    return "\n".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────

def _file_status(path: str, diff_result: Optional[DiffResult]) -> str:
    if diff_result is None:
        return "MODIFIED"
    fd = diff_result.files.get(path)
    return fd.status.upper() if fd else "MODIFIED"


def _token_estimate(text: str) -> int:
    return len(text) // 4


_tok = _token_estimate
