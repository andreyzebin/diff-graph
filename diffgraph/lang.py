from __future__ import annotations
import os

# File extension → language id. The language id is the canonical
# name used everywhere downstream (outline._TS_LANG / _TS_MODULES,
# DECLARATION_PATTERNS, _LANG_GLOBS, …) — keep it stable.
#
# A language listed here gets file-detection + glob support out of
# the box; whether the OUTLINE works depends on whether
# `diffgraph/outline.py` has _CONTAINERS / _MEMBERS / _TS_MODULES
# entries for it. Pure file types (json/yaml/html/css) have no
# outline contract — they parse fine but no code structure to walk.
LANG_MAP = {
    # JVM
    ".java":  "java",
    ".kt":    "kotlin",
    ".kts":   "kotlin",
    ".scala": "scala",
    ".sc":    "scala",
    # Web frontend
    ".js":    "javascript",
    ".jsx":   "javascript",
    ".mjs":   "javascript",
    ".cjs":   "javascript",
    ".ts":    "typescript",
    ".tsx":   "typescript",
    # Python
    ".py":    "python",
    ".pyi":   "python",
    # Go
    ".go":    "go",
    # Systems
    ".rs":    "rust",
    ".c":     "c",
    ".h":     "c",
    ".cpp":   "cpp",
    ".cc":    "cpp",
    ".cxx":   "cpp",
    ".hpp":   "cpp",
    ".hxx":   "cpp",
    # .NET
    ".cs":    "csharp",
    # PHP
    ".php":   "php",
    # Ruby
    ".rb":    "ruby",
    # Shell
    ".sh":    "bash",
    ".bash":  "bash",
    # Markup / config (no outline; useful for grep / file detection)
    ".html":  "html",
    ".htm":   "html",
    ".css":   "css",
    ".scss":  "css",
    ".json":  "json",
    ".yaml":  "yaml",
    ".yml":   "yaml",
}

# Patterns for declaration search via search_text — used by tools
# that fall back to grep when tree-sitter isn't available or when
# the language has no outline support.
DECLARATION_PATTERNS: dict[str, list[str]] = {
    "java":       ["class {name}", "interface {name}", "enum {name}", "record {name}"],
    "kotlin":     ["class {name}", "interface {name}", "object {name}"],
    "scala":      ["class {name}", "object {name}", "trait {name}"],
    "javascript": ["class {name}", "function {name}"],
    "typescript": ["class {name}", "interface {name}", "type {name}", "function {name}"],
    "python":     ["class {name}", "def {name}"],
    "go":         ["type {name} struct", "type {name} interface", "func {name}", "func (.*) {name}"],
    "rust":       ["struct {name}", "enum {name}", "trait {name}", "impl {name}", "fn {name}"],
    "c":          ["struct {name}", "{name}("],
    "cpp":        ["class {name}", "struct {name}", "namespace {name}"],
    "csharp":     ["class {name}", "interface {name}", "enum {name}", "struct {name}", "record {name}"],
    "php":        ["class {name}", "interface {name}", "trait {name}", "function {name}"],
    "ruby":       ["class {name}", "module {name}", "def {name}"],
    "bash":       ["{name}()", "function {name}"],
}

# File extensions for each language — inverse of LANG_MAP, used by
# `repo_list` / `diff_list_files` filters when callers want
# "show me all the Python files in the diff".
FILE_EXTENSIONS: dict[str, list[str]] = {
    "java":       [".java"],
    "kotlin":     [".kt", ".kts"],
    "scala":      [".scala", ".sc"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "python":     [".py", ".pyi"],
    "go":         [".go"],
    "rust":       [".rs"],
    "c":          [".c", ".h"],
    "cpp":        [".cpp", ".cc", ".cxx", ".hpp", ".hxx"],
    "csharp":     [".cs"],
    "php":        [".php"],
    "ruby":       [".rb"],
    "bash":       [".sh", ".bash"],
    "html":       [".html", ".htm"],
    "css":        [".css", ".scss"],
    "json":       [".json"],
    "yaml":       [".yaml", ".yml"],
}


def detect_lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return LANG_MAP.get(ext, "unknown")


def get_search_patterns(name: str, lang: str) -> list[str]:
    patterns = DECLARATION_PATTERNS.get(lang, [])
    return [p.format(name=name) for p in patterns]


def get_extensions(lang: str) -> list[str]:
    return FILE_EXTENSIONS.get(lang, [])


# Glob patterns for searching source files by language — used by
# `diff_search(glob=...)` callers that want a language-typed search
# without enumerating every extension.
_LANG_GLOBS: dict[str, list[str]] = {
    lang: [f"**/*{ext}" for ext in exts]
    for lang, exts in FILE_EXTENSIONS.items()
}


def get_globs_for_lang(lang: str) -> list[str]:
    """Return glob patterns for all source files of the given language."""
    return _LANG_GLOBS.get(lang, ["**/*"])
