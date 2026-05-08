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
    from .tools import list_files as _list_files_impl, read_file, search_text
    from .outline import get_outline
    from .comment_tools import (
        list_threads as _list_threads_impl,
        read_thread as _read_thread_impl,
        read_comment as _read_comment_impl,
        snapshot_max_id as _snapshot_max_id,
    )

    def _ensure():
        """Trigger lazy clone if needed."""
        ctx.ensure_repo()

    def _comment_snapshot() -> int:
        """Cache the run-start max_comment_id on ctx the first time
        a comment tool is called. After that the value is fixed —
        any post_comment we make during the run gets a higher id and
        becomes invisible to these tools (consistent snapshot)."""
        v = getattr(ctx, "_comment_snapshot_max_id", None)
        if v is None:
            v = _snapshot_max_id(ctx.existing_comments or [])
            ctx._comment_snapshot_max_id = v
        return v

    def _bot_user() -> str:
        return getattr(ctx, "_bot_user", "") or ""

    def _subject_pattern():
        return getattr(ctx, "_subject_pattern", None)

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
            ),
            "commits": _get_commit_list(ctx.repo_path, ctx.base_ref, ctx.source_ref)
                       if ctx.base_ref and ctx.source_ref else "(unavailable)",
        }

    def _default_ref() -> str:
        """Compute default ref dynamically (after lazy init may have set base/source)."""
        return "base..source" if (ctx.base_ref and ctx.source_ref) else "source"

    @registry.register(
        name="list_files",
        description=(
            "List paths in the diff view (default `ref=base..source`) — "
            "every file visible from the source side, whether added, "
            "modified, or unchanged. Returns up to 50 relative paths."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob, e.g. '**/*.java'. Default '**/*' = everything."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
            },
            "required": ["pattern"],
        },
    )
    def list_files(pattern: str = "**/*", ref: str = "") -> list[str]:
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import list_files_vfs
            files = list_files_vfs(vfs_dir, pattern)
        else:
            files = _list_files_impl(pattern, ctx.repo_path)
        return [f for f in files if not _skip_dir(f)][:50]

    @registry.register(
        name="read_file",
        description=(
            "Read a file from the diff view as unified diff: every line "
            "carries a `+`/`-`/` ` marker plus its old (base) and new "
            "(source) line numbers. Without a range, returns the full "
            "file annotated. With `changes_only=true`, returns only the "
            "changed hunks ±`before`/`after` context lines. With "
            "`ref=\"source\"`, returns plain file content without markers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "L position (1-indexed, inclusive). L = position in the unified-diff view, as shown by read_outline."},
                "end_line": {"type": "integer", "description": "L position (1-indexed, inclusive)."},
                "changes_only": {"type": "boolean", "description": "Collapse output to changed lines with ±context."},
                "before": {"type": "integer", "description": "Context lines before each hunk when changes_only=true (default 3)."},
                "after": {"type": "integer", "description": "Context lines after each hunk when changes_only=true (default 3)."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
            },
            "required": ["path"],
        },
    )
    def read_file_tool(path: str = "", start_line: int = None, end_line: int = None,
                       changes_only: bool = False, before: int = 3, after: int = 3,
                       ref: str = "") -> str:
        _ensure()
        ref = ref or _default_ref()
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
        description=(
            "Structural outline (classes, methods, fields) of a file in "
            "the diff view. Each symbol shows L range (use for read_file "
            "ranges) and old/new ranges (for reference). Changed symbols "
            "are marked `*`; for changed methods the outline shows "
            "separate `Lold:..` and `Lnew:..` so you can read the old or "
            "new version individually."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
            },
            "required": ["path"],
        },
    )
    def read_outline_tool(path: str = "", ref: str = "") -> str:
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import read_outline_vfs
            return read_outline_vfs(vfs_dir, path, repo_path=ctx.repo_path)
        fd = ctx.diff_result.files.get(path)
        changed = set(fd.after_changed_lines) if fd else None
        return get_outline(path, ctx.repo_path, changed)

    @registry.register(
        name="search",
        description=(
            "Search a string or regex across files in the diff view. Each "
            "hit is returned with its `+`/`-`/` ` marker and L/old/new "
            "coordinates — you see added, deleted, and unchanged "
            "occurrences in one query. Results are grouped by file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": "string", "description": "File filter, e.g. '**/*.java'. Default '**/*'."},
                "regex": {"type": "boolean", "description": "Treat query as regex."},
                "before": {"type": "integer", "description": "Context lines before each match (default 0)."},
                "after": {"type": "integer", "description": "Context lines after each match (default 0)."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
            },
            "required": ["query"],
        },
    )
    def search_tool(query: str = "", glob: str = "**/*", regex: bool = False,
                    before: int = 0, after: int = 0, ref: str = "") -> str:
        _ensure()
        ref = ref or _default_ref()
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

    # ── Comment-graph navigation (replaces baked EXISTING COMMENTS) ──────

    @registry.register(
        name="list_threads",
        description=(
            "List the PR's root comment threads — orientation across the "
            "discussion. Each row is one line with id, author, reply count, "
            "and the first line of the root body. Snapshot at run start: "
            "comments posted by the agent itself during the run are not "
            "shown here. Use `read_thread(id)` to drill into a specific one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "Pagination offset (default 0)."},
                "n": {"type": "integer", "description": "Page size (default 30)."},
                "sort": {
                    "type": "string",
                    "enum": ["newest", "most_active", "oldest"],
                    "description": "Order. 'newest' (default) = recent root first; 'most_active' = most replies; 'oldest' = chronological.",
                },
            },
            "required": [],
        },
    )
    def list_threads_tool(start: int = 0, n: int = 30, sort: str = "newest") -> str:
        return _list_threads_impl(
            ctx.existing_comments or [],
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
            start=start,
            n=n,
            sort=sort,
        )

    @registry.register(
        name="read_thread",
        description=(
            "Render the FULL thread containing the given comment, depth-first "
            "from the root. Pass any comment id — root, leaf, or middle of the "
            "tree; the tool finds the root and walks the subtree. Each comment "
            "appears as a header block (`=== #id by author · reply to #X · …`) "
            "followed by its verbatim body (markdown / code blocks preserved). "
            "Long bodies and deep trees are truncated with explicit hints to "
            "call `read_comment(id)` or `read_thread(<sub_id>)` to expand."
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer", "description": "Any comment id in the thread of interest."},
            },
            "required": ["comment_id"],
        },
    )
    def read_thread_tool(comment_id: int = 0) -> str:
        if not comment_id:
            return "(comment_id is required)"
        return _read_thread_impl(
            ctx.existing_comments or [],
            comment_id=comment_id,
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
        )

    @registry.register(
        name="read_comment",
        description=(
            "Render ONE specific comment in full, no caps. Use when "
            "`read_thread` truncated a body and you need the rest."
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer"},
            },
            "required": ["comment_id"],
        },
    )
    def read_comment_tool(comment_id: int = 0) -> str:
        if not comment_id:
            return "(comment_id is required)"
        return _read_comment_impl(
            ctx.existing_comments or [],
            comment_id=comment_id,
            snapshot_max_id_value=_comment_snapshot(),
            bot_user=_bot_user(),
            subject_pattern=_subject_pattern(),
        )

    @registry.register(
        name="post_comment",
        description=(
            "Post a single comment on the PR. One tool covers all three "
            "shapes that share Bitbucket's comments endpoint:\n"
            "- general comment: just `text`.\n"
            "- inline finding: `text` + `file` + `line` (+ optional `severity`).\n"
            "- reply to existing thread: `text` + `parent_id`.\n"
            "Severity is one of BLOCKER / MAJOR / MINOR / COMMENT and only "
            "matters for inline comments. Returns the posted comment id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "file": {"type": "string", "description": "Relative path; pair with `line` for an inline anchor."},
                "line": {"type": "integer", "description": "1-indexed line in the changed (right) version."},
                "severity": {
                    "type": "string",
                    "enum": ["BLOCKER", "MAJOR", "MINOR", "COMMENT"],
                    "description": "Only meaningful for inline comments.",
                },
                "parent_id": {"type": "integer", "description": "Comment id to reply under."},
            },
            "required": ["text"],
        },
    )
    def post_comment_tool(text: str = "",
                          file: str = "",
                          line: int = 0,
                          severity: str = "",
                          parent_id: int = 0) -> dict:
        pr_url = getattr(ctx, "_pr_url", "") or ""
        if not pr_url:
            return {"status": "skipped",
                    "message": "no pr_url on ctx; running locally — comments not posted"}
        if not text or not text.strip():
            return {"status": "error", "message": "text is required"}

        decorate = getattr(ctx, "_decorate", None)
        author_prefix = getattr(ctx, "_author_prefix", "") or ""
        body = text
        if author_prefix and not body.lstrip().startswith(author_prefix):
            body = f"{author_prefix} {body}"
        if decorate:
            body = decorate(body)

        # Best-effort line snap when we have a diff result and the agent
        # picked a line that's near (but not exactly on) a changed line.
        snapped_line = int(line or 0)
        if file and snapped_line and ctx._initialized and ctx.diff_result is not None:
            fd = ctx.diff_result.files.get(file)
            if fd is not None:
                changed = fd.after_changed_lines
                if changed and snapped_line not in changed:
                    snapped_line = min(changed, key=lambda L: abs(L - snapped_line))

        from diffgraph.bitbucket import post_pr_comment
        try:
            cid = post_pr_comment(
                pr_url,
                text=body,
                file=file or "",
                line=snapped_line,
                severity=severity or "",
                parent_id=int(parent_id or 0),
            )
        except Exception as exc:
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

        mode = "reply" if parent_id else ("inline" if (file and line) else "general")
        ctx.review_context.posted_comments.append({
            "comment_id": cid, "mode": mode,
            "file": file, "line": snapped_line,
            "parent_id": int(parent_id or 0),
        })
        return {"status": "posted", "comment_id": cid, "mode": mode}

    @registry.register(
        name="react_to_comment",
        description=(
            "Add a reaction emoji to an existing PR comment. The agent's "
            "lightweight way to acknowledge a thread without writing a "
            "reply: thumbs_up for 'addressed / agree', thumbs_down for "
            "'still not OK', eyes for 'looking into this', tada for "
            "'fixed nicely', etc. Use this in place of a verbal 'resolved' "
            "reply when the diff already speaks for itself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer"},
                "emoticon": {
                    "type": "string",
                    "description": (
                        "Reaction name without colons. Common values: "
                        "thumbs_up, thumbs_down, heart, smile, tada, "
                        "confused, eyes, rocket."
                    ),
                },
            },
            "required": ["comment_id", "emoticon"],
        },
    )
    def react_to_comment_tool(comment_id: int = 0, emoticon: str = "") -> dict:
        pr_url = getattr(ctx, "_pr_url", "") or ""
        if not pr_url:
            return {"status": "skipped",
                    "message": "no pr_url on ctx; running locally — reaction not posted"}
        if not comment_id:
            return {"status": "error", "message": "comment_id is required"}
        from diffgraph.bitbucket import react_to_pr_comment
        try:
            react_to_pr_comment(pr_url, int(comment_id), emoticon)
            return {"status": "posted",
                    "comment_id": int(comment_id),
                    "emoticon": emoticon.strip().strip(":")}
        except Exception as exc:
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    @registry.register(
        name="set_review_status",
        description=(
            "Set the agent's overall verdict on the PR. Use APPROVED when no "
            "blocking issues are present, NEEDS_WORK when at least one "
            "BLOCKER or MAJOR finding stands, UNAPPROVED to clear a prior "
            "decision. Strictness — when to approve vs request changes — is "
            "guided by the agent prompt, not this tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["APPROVED", "NEEDS_WORK", "UNAPPROVED"],
                },
                "reason": {
                    "type": "string",
                    "description": "One-sentence explanation, recorded for audit.",
                },
            },
            "required": ["status"],
        },
    )
    def set_review_status_tool(status: str = "", reason: str = "") -> dict:
        normalised = (status or "").strip().upper()
        if normalised not in ("APPROVED", "NEEDS_WORK", "UNAPPROVED"):
            return {"status": "error",
                    "message": f"unknown status {status!r}; expected APPROVED / NEEDS_WORK / UNAPPROVED"}
        ctx.review_context.review_status = normalised
        ctx.review_context.review_status_reason = reason or ""
        return {"status": "queued", "review_status": normalised}
