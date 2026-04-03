from __future__ import annotations
from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str) -> str:
    """Load a prompt template by filename (without path)."""
    return (_DIR / name).read_text(encoding="utf-8")
