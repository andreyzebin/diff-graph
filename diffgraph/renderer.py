from __future__ import annotations
from typing import Optional

from .diff_parser import DiffResult
from .model import MetaModel, Module, Symbol


def render(
    model: MetaModel,
    diff_result: Optional[DiffResult] = None,
    max_tokens: int = 8000,
) -> str:
    """
    Render a MetaModel as a text prompt context.

    Detail levels:
    - changed symbol  : before_code + after full_code + signature + summary
    - depth 0 (unchanged symbols): signature + summary
    - depth 1         : all symbols — signature + summary
    - depth 2         : module summary only

    Token budget (rough estimate: len // 4):
    If over limit, degrades depth=2 → names only, then depth=1 → summary only.
    Changed modules are never truncated.
    """
    # Separate modules by role
    changed_ids = set(model.changed_module_ids)
    depth1 = [m for m in model.modules.values() if m.depth == 1]
    depth2 = [m for m in model.modules.values() if m.depth >= 2]

    def _try_render(trunc_depth2: bool, trunc_depth1: bool) -> str:
        parts: list[str] = []

        # ── Changed modules ──────────────────────────────────────────────
        changed_modules = [model.modules[mid] for mid in model.changed_module_ids if mid in model.modules]
        if changed_modules:
            parts.append("## Changed Modules\n")
            for mod in changed_modules:
                status = _file_status(mod.id, diff_result)
                parts.append(f"### {mod.id} [{status}]")
                parts.append(f'> "{mod.summary}"')
                parts.append("")
                _render_changed_module(mod, parts)
                parts.append("---\n")

        # ── Direct dependencies (depth 1) ─────────────────────────────
        direct_deps = [m for m in depth1 if m.id not in changed_ids]
        if direct_deps:
            parts.append("## Direct Dependencies (depth 1)\n")
            for mod in direct_deps:
                parts.append(f"### {mod.id}")
                parts.append(f'> "{mod.summary}"')
                if trunc_depth1:
                    parts.append(f"> {mod.summary}")
                else:
                    for sym in mod.symbols:
                        parts.append(f"- [{sym.kind}] {sym.signature}")
                        if sym.summary:
                            parts.append(f"  > \"{sym.summary}\"")
                parts.append("")

        # ── Transitive dependencies (depth 2+) ───────────────────────
        transitive = [m for m in depth2 if m.id not in changed_ids]
        if transitive:
            parts.append("## Transitive Dependencies (depth 2)\n")
            for mod in transitive:
                if trunc_depth2:
                    parts.append(f"### {mod.name} — [name only]")
                else:
                    parts.append(f'### {mod.id} — "{mod.summary}" [summary only]')

        return "\n".join(parts)

    # Try full render first
    full = _try_render(trunc_depth2=False, trunc_depth1=False)
    if _token_estimate(full) <= max_tokens:
        return full

    # Degrade depth 2
    degraded = _try_render(trunc_depth2=True, trunc_depth1=False)
    if _token_estimate(degraded) <= max_tokens:
        return degraded

    # Degrade depth 1 too
    return _try_render(trunc_depth2=True, trunc_depth1=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _render_changed_module(mod: Module, parts: list[str]) -> None:
    changed = [s for s in mod.symbols if s.is_changed]
    unchanged = [s for s in mod.symbols if not s.is_changed]

    if changed:
        parts.append("Modified symbols:")
        for sym in changed:
            ann = " ".join(sym.annotations)
            sig_line = f"- [{sym.kind}] {sym.signature}"
            if ann:
                sig_line += f"  {ann}"
            parts.append(sig_line)
            if sym.summary:
                parts.append(f'  > "{sym.summary}"')
            parts.append("")

            lang = mod.lang
            if sym.before_code is not None:
                parts.append("  BEFORE:")
                parts.append(f"  ```{lang}")
                for line in sym.before_code.splitlines():
                    parts.append(f"  {line}")
                parts.append("  ```")
                parts.append("")

            if sym.full_code is not None:
                parts.append("  AFTER:")
                parts.append(f"  ```{lang}")
                for line in sym.full_code.splitlines():
                    parts.append(f"  {line}")
                parts.append("  ```")
                parts.append("")

    if unchanged:
        parts.append("Other symbols:")
        for sym in unchanged:
            parts.append(f"- [{sym.kind}] {sym.signature}")
            if sym.summary:
                parts.append(f"  > \"{sym.summary}\"")
        parts.append("")


def _file_status(path: str, diff_result: Optional[DiffResult]) -> str:
    if diff_result is None:
        return "MODIFIED"
    fd = diff_result.files.get(path)
    if fd is None:
        return "MODIFIED"
    return fd.status.upper()


def _token_estimate(text: str) -> int:
    return len(text) // 4
