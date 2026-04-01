from __future__ import annotations
import os

LANG_MAP = {
    ".java":  "java",
    ".py":    "python",
    ".ts":    "typescript",
    ".tsx":   "typescript",
    ".go":    "go",
    ".kt":    "kotlin",
    ".rb":    "ruby",
    ".cs":    "csharp",
}

# Patterns for declaration search via search_text
DECLARATION_PATTERNS: dict[str, list[str]] = {
    "java":       ["class {name}", "interface {name}", "enum {name}"],
    "python":     ["class {name}"],
    "typescript": ["class {name}", "interface {name}", "type {name}"],
    "go":         ["type {name} struct", "type {name} interface"],
    "kotlin":     ["class {name}", "interface {name}", "object {name}"],
    "ruby":       ["class {name}", "module {name}"],
    "csharp":     ["class {name}", "interface {name}", "enum {name}"],
}

# File extensions for each language
FILE_EXTENSIONS: dict[str, list[str]] = {
    "java":       [".java"],
    "python":     [".py"],
    "typescript": [".ts", ".tsx"],
    "go":         [".go"],
    "kotlin":     [".kt"],
    "ruby":       [".rb"],
    "csharp":     [".cs"],
}


def detect_lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return LANG_MAP.get(ext, "unknown")


def get_search_patterns(name: str, lang: str) -> list[str]:
    patterns = DECLARATION_PATTERNS.get(lang, [])
    return [p.format(name=name) for p in patterns]


def get_extensions(lang: str) -> list[str]:
    return FILE_EXTENSIONS.get(lang, [])
