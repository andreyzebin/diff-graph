"""
Prompt template loading and variable interpolation.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


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


# Matches {word} but not {word:spec} or {"json"} or {<angle>} etc.
# Only replaces simple {identifier} placeholders.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def interpolate(template: str, **variables: Any) -> str:
    """
    Replace {var_name} placeholders with values.

    Only replaces simple {identifier} patterns — leaves JSON braces,
    format specs, and other brace content untouched.
    """
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return str(variables[key])
        return match.group(0)  # leave as-is if not in variables

    return _PLACEHOLDER_RE.sub(_replace, template)
