"""
Register diffgraph's domain-specific tools in an Orchestra ToolRegistry.

Each tool is a closure over the review context (_Ctx). When a VFS directory
is available (base_ref + source_ref), tools operate on the virtual unified
diff filesystem. Otherwise they fall back to plain filesystem access.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from orchestra.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .orchestrator import _Ctx

_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "vendor",
    "dist", "build", ".git", "__pycache__", ".mypy_cache",
}


def _skip_dir(path: str) -> bool:
    return any(p in _SKIP_DIRS for p in path.replace("\\", "/").split("/"))


def register_diffgraph_tools(registry: ToolRegistry, ctx: "_Ctx") -> None:
    """Register all 7 diffgraph domain tools."""
    from .tools import list_files, read_file, search_text
    from .outline import get_outline

    use_vfs = bool(ctx.vfs_dir)

    if use_vfs:
        from diffsearch.tools import (
            read_file_vfs,
            search_vfs,
            list_files_vfs,
            read_outline_vfs,
        )
        from diffsearch.virtual_fs import load_diffmeta

    @registry.register(
        name="find_files",
        description="List files matching a glob pattern. Returns relative paths.",
        parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    )
    def find_files(pattern: str = "**/*") -> list[str]:
        if use_vfs:
            files = list_files_vfs(ctx.vfs_dir, pattern)
        else:
            files = list_files(pattern, ctx.repo_path)
        return [f for f in files if not _skip_dir(f)][:50]

    @registry.register(
        name="read_file",
        description="Read up to 100 lines of a file. Shows old/new line numbers and +/- markers for changed files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-indexed inclusive (L position in unified view)."},
                "end_line": {"type": "integer", "description": "1-indexed inclusive."},
            },
            "required": ["path"],
        },
    )
    def read_file_tool(path: str = "", start_line: int = None, end_line: int = None) -> str:
        if start_line is not None and end_line is not None and (end_line - start_line) > 100:
            end_line = start_line + 99
        if use_vfs:
            return read_file_vfs(
                ctx.vfs_dir, path,
                start_line=start_line or 1,
                end_line=end_line,
            )
        return read_file(path, ctx.repo_path, start_line, end_line) or "(file not found)"

    @registry.register(
        name="read_outline",
        description=(
            "Get the structural outline of a file — classes, methods, line ranges. "
            "Changed symbols marked with *. Use this before read_file to orient yourself."
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    def read_outline_tool(path: str = "") -> str:
        if use_vfs:
            return read_outline_vfs(ctx.vfs_dir, path, repo_path=ctx.repo_path)
        fd = ctx.diff_result.files.get(path)
        changed = set(fd.after_changed_lines) if fd else None
        return get_outline(path, ctx.repo_path, changed)

    @registry.register(
        name="search",
        description="Search for a string or regex across repo files. Finds both old (deleted) and new (added) code.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": "string", "description": "File filter, e.g. '**/*.java'."},
                "regex": {"type": "boolean"},
                "before": {"type": "integer", "description": "Context lines before each match."},
                "after": {"type": "integer", "description": "Context lines after each match."},
            },
            "required": ["query"],
        },
    )
    def search_tool(query: str = "", glob: str = "**/*", regex: bool = False,
                    before: int = 0, after: int = 0) -> str:
        if use_vfs:
            return search_vfs(
                ctx.vfs_dir, query, glob=glob, regex=regex,
                before=before, after=after,
            )
        results = search_text(query, ctx.repo_path, glob=glob, regex=regex)
        filtered = [r for r in results if not _skip_dir(r.file)][:30]
        if not filtered:
            return "(no matches)"
        lines = []
        for r in filtered:
            lines.append(f"{r.file}:{r.line}: {r.text}")
        return "\n".join(lines)

    @registry.register(
        name="get_diff",
        description="Get the full diff or the diff section for a specific file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional: filter to one file."},
            },
            "required": [],
        },
    )
    def get_diff_tool(path: str = None) -> str:
        if path:
            fd = ctx.diff_result.files.get(path)
            if fd is None:
                return f"No diff section found for {path}"
            return _extract_file_diff(path, ctx.diff_text)
        text = ctx.diff_text
        if len(text) > 8000:
            text = text[:8000] + "\n... (truncated, use path= to get a specific file)"
        return text

    @registry.register(
        name="reply_to_comment",
        description="Reply to an existing PR review comment thread.",
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["comment_id", "text"],
        },
    )
    def reply_to_comment_tool(comment_id: int = 0, text: str = "") -> dict:
        ctx.review_context.comment_replies.append({
            "comment_id": comment_id,
            "text": text,
        })
        return {"status": "queued"}

    @registry.register(
        name="resolve_comment",
        description="Mark an existing PR comment thread as resolved (issue addressed in this diff).",
        parameters={
            "type": "object",
            "properties": {"comment_id": {"type": "integer"}},
            "required": ["comment_id"],
        },
    )
    def resolve_comment_tool(comment_id: int = 0) -> dict:
        ctx.review_context.comment_resolves.append(comment_id)
        return {"status": "queued"}


def _extract_file_diff(path: str, diff_text: str) -> str:
    out: list[str] = []
    in_file = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git") and f" b/{path}" in line:
            in_file = True
        elif line.startswith("diff --git") and in_file:
            break
        if in_file:
            out.append(line)
    result = "\n".join(out)
    if len(result) > 6000:
        result = result[:6000] + "\n... (truncated)"
    return result or f"No diff section found for {path}"
