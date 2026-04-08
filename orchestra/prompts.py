"""
Prompt template loading and variable interpolation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def load_prompt(path_or_text: str, base_dir: Path | None = None) -> str:
    """If path_or_text points to an existing file, load it; otherwise return as-is."""
    if not path_or_text:
        return ""
    if base_dir:
        candidate = base_dir / path_or_text
    else:
        candidate = Path(path_or_text)
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return path_or_text


def interpolate(template: str, **variables: Any) -> str:
    """
    Replace {var_name} placeholders with values.

    Uses str.format_map with a defaultdict fallback so missing keys
    are left as-is (no KeyError).
    """
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(_SafeDict(**variables))
