"""
Prompt template file loading.

Variable interpolation lives in `orchestra.template_engine` (Jinja).
This module is only about reading template text from the filesystem.
"""
from __future__ import annotations

from pathlib import Path


def load_prompt(path_or_text: str, base_dir: Path | None = None) -> str:
    """If path_or_text points to an existing file, load it; otherwise return as-is."""
    if not path_or_text:
        return ""
    # If it contains newlines or is very long, it's already prompt text, not a path
    if "\n" in path_or_text or len(path_or_text) > 500:
        return path_or_text
    try:
        if base_dir:
            candidate = base_dir / path_or_text
        else:
            candidate = Path(path_or_text)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError:
        pass
    return path_or_text


_INTERNAL_DIR = Path(__file__).parent / "internal"
_INTERNAL_CACHE: dict[str, str] = {}


def load_internal(name: str) -> str:
    """Load a framework-internal prompt fragment from
    `orchestra/prompts/internal/<name>.md`. These are the strings the
    framework injects outside the agent's own .user.md template —
    pushers, force-done, handoff envelopes, condensation summaries,
    parallel-merge prompts, etc.

    Centralised here so they're editable as plain Markdown alongside
    agent prompts and visible in audit (see docs/internal-prompts.md).

    Cached after first read. Returns the raw template; callers run it
    through `orchestra.template_engine.render(text, ctx)` to
    substitute placeholders against a RunContext.
    """
    if name in _INTERNAL_CACHE:
        return _INTERNAL_CACHE[name]
    path = _INTERNAL_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"internal prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    # Files use a final newline as part of the markdown convention;
    # callers want the prompt exactly as written so we strip a single
    # trailing newline (multi-line bodies stay intact).
    if text.endswith("\n"):
        text = text[:-1]
    _INTERNAL_CACHE[name] = text
    return text
