"""
Register diffgraph's domain-specific tools in an Orchestra ToolRegistry.

Each tool is a closure over the review context (_Ctx). Tools accept an
optional `ref` parameter for selecting the diff view:
  - "base..source" (default) — unified diff VFS
  - "<sha1>..<sha2>" — VFS for specific commit range
  - "source" — plain filesystem, no diff markers
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestra.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .orchestrator import _Ctx

log = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "vendor",
    "dist", "build", ".git", "__pycache__", ".mypy_cache",
}


def _skip_dir(path: str) -> bool:
    return any(p in _SKIP_DIRS for p in path.replace("\\", "/").split("/"))


def _resolve_ref(ctx: "_Ctx", ref: str) -> tuple[str, str] | None:
    """Resolve abstract ref names to SHA pair. Returns None for plain mode."""
    if ".." not in ref:
        return None  # plain mode (ref="source" or single SHA)
    left, right = ref.split("..", 1)
    left = ctx.base_ref if left == "base" else left
    right = ctx.source_ref if right == "source" else right
    if not left or not right:
        return None
    return left, right


def _get_vfs(ctx: "_Ctx", ref: str) -> str | None:
    """Get or create VFS directory for a ref range. Returns None for plain mode."""
    resolved = _resolve_ref(ctx, ref)
    if not resolved:
        return None

    # Cache key is the resolved SHA pair
    cache_key = f"{resolved[0]}..{resolved[1]}"
    if cache_key not in ctx.vfs_cache:
        from diffsearch.virtual_fs import materialize_vfs
        vfs_dir = materialize_vfs(ctx.repo_path, resolved[0], resolved[1])
        ctx.vfs_cache[cache_key] = vfs_dir
        log.info("VFS materialized for %s: %s", cache_key[:16], vfs_dir)
    return ctx.vfs_cache[cache_key]


def register_diffgraph_tools(registry: ToolRegistry, ctx: "_Ctx") -> None:
    """Register all diffgraph domain tools. Tools call ctx.ensure_repo() lazily."""
    from .tools import list_files, read_file, search_text
    from .outline import get_outline

    def _ensure():
        """Trigger lazy clone if needed."""
        ctx.ensure_repo()

    # ── Data provider: pr_context (cached, hidden) ────────────────────────

    @registry.register(
        name="pr_context",
        description="PR context data provider (diff summary, comments, commits).",
        hidden=True,
        cache=True,
    )
    def pr_context() -> dict:
        _ensure()
        from .orchestrator import _make_diff_summary, _get_commit_list, _format_existing_comments
        return {
            "diff_summary": _make_diff_summary(ctx.diff_result),
            "existing_comments": _format_existing_comments(
                ctx.existing_comments,
                bot_user=getattr(ctx, '_bot_user', ''),
                max_comments=20,
            ),
            "commits": _get_commit_list(ctx.repo_path, ctx.base_ref, ctx.source_ref)
                       if ctx.base_ref and ctx.source_ref else "(unavailable)",
        }

    # Default ref when base/source are available
    default_ref = "base..source" if (ctx.base_ref and ctx.source_ref) else "source"

    @registry.register(
        name="find_files",
        description="List files matching a glob pattern. Returns relative paths.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default), "<sha>..<sha>", or "source" for plain.'},
            },
            "required": ["pattern"],
        },
    )
    def find_files(pattern: str = "**/*", ref: str = default_ref) -> list[str]:
        _ensure()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import list_files_vfs
            files = list_files_vfs(vfs_dir, pattern)
        else:
            files = list_files(pattern, ctx.repo_path)
        return [f for f in files if not _skip_dir(f)][:50]

    @registry.register(
        name="read_file",
        description="Read file with diff markers (+/-). Use changes_only=true to see just the changed hunks.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-indexed inclusive (L position)."},
                "end_line": {"type": "integer", "description": "1-indexed inclusive."},
                "changes_only": {"type": "boolean", "description": "Show only changed lines with context."},
                "before": {"type": "integer", "description": "Context lines before changes (default 3)."},
                "after": {"type": "integer", "description": "Context lines after changes (default 3)."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default), "<sha>..<sha>", or "source" for plain.'},
            },
            "required": ["path"],
        },
    )
    def read_file_tool(path: str = "", start_line: int = None, end_line: int = None,
                       changes_only: bool = False, before: int = 3, after: int = 3,
                       ref: str = default_ref) -> str:
        _ensure()
        if not changes_only and start_line is not None and end_line is not None and (end_line - start_line) > 100:
            end_line = start_line + 99
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import read_file_vfs
            return read_file_vfs(
                vfs_dir, path,
                start_line=start_line or 1,
                end_line=end_line,
                changes_only=changes_only,
                context_before=before,
                context_after=after,
            )
        return read_file(path, ctx.repo_path, start_line, end_line) or "(file not found)"

    @registry.register(
        name="read_outline",
        description="Structural outline — classes, methods, line ranges. Changed symbols marked with *.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default), "<sha>..<sha>", or "source" for plain.'},
            },
            "required": ["path"],
        },
    )
    def read_outline_tool(path: str = "", ref: str = default_ref) -> str:
        _ensure()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import read_outline_vfs
            return read_outline_vfs(vfs_dir, path, repo_path=ctx.repo_path)
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
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default), "<sha>..<sha>", or "source" for plain.'},
            },
            "required": ["query"],
        },
    )
    def search_tool(query: str = "", glob: str = "**/*", regex: bool = False,
                    before: int = 0, after: int = 0, ref: str = default_ref) -> str:
        _ensure()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import search_vfs
            return search_vfs(
                vfs_dir, query, glob=glob, regex=regex,
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
        description="Get the full diff or the diff section for a specific file (legacy — prefer read_file with changes_only=true).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional: filter to one file."},
            },
            "required": [],
        },
    )
    def get_diff_tool(path: str = None) -> str:
        _ensure()
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
