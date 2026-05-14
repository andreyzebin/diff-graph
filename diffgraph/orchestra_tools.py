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


def _path_from_listing_row(row: str) -> str:
    """Extract the file path from a diff_list_files row.
    Row shape: `<X>  <path>  (<summary>)` — two spaces after status,
    two spaces before summary. We slice off the 3-char status prefix
    and trim trailing summary."""
    body = row[3:] if len(row) > 3 else row
    cut = body.rfind("  (")
    return (body[:cut] if cut > 0 else body).rstrip()


def _filter_and_page_listing(text: str, start: int, n: int) -> str:
    """Drop rows whose path is inside a skip-dir, then slice
    `[start : start+n]`. Path-width alignment from list_files_vfs is
    preserved — we only filter and slice, never re-align (column
    widths are sized once at format time, so trimming rows just
    leaves consistent trailing whitespace, which is fine in
    monospace). Appends a pagination footer when there are more rows
    than the page shows."""
    if not text:
        return ""
    rows = [r for r in text.splitlines()
            if not _skip_dir(_path_from_listing_row(r))]
    total = len(rows)
    page = rows[start : start + n]
    out = "\n".join(page)
    # Footer when there's more to see — agent knows to paginate.
    if total > start + n or start > 0:
        out += (
            f"\n\n[showing {start}..{start + len(page) - 1} of {total} — "
            f"call with start={start + n} to see the next page]"
        )
    return out


def _format_plain_listing(paths: list[str], repo_path: str) -> str:
    """Format a plain (non-VFS, `ref="source"`) listing with the same
    shape as list_files_vfs but no diff metadata — status is always
    space, summary is `(<L>L · <bytes>)`."""
    from diffsearch.tools import _fmt_bytes
    if not paths:
        return ""
    from pathlib import Path as _P
    rows: list[tuple[str, str]] = []
    for rel in paths:
        p = _P(repo_path) / rel
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                L = sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            size = 0
            L = 0
        rows.append((rel, f"({L}L · {_fmt_bytes(size)})"))
    path_w = max(len(r[0]) for r in rows)
    return "\n".join(f"   {path:<{path_w}}  {summary}"
                     for path, summary in rows)


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
        description="PR context data provider (comments, commits).",
        hidden=True,
        cache=True,
    )
    def pr_context() -> dict:
        _ensure()
        from .orchestrator import _get_commit_list, _format_existing_comments
        return {
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
        name="diff_list_files",
        description=(
            "List paths in the diff view (default `ref=base..source`) "
            "as plain text, one row per file with a status code and a "
            "size summary so you can budget reads before calling "
            "`diff_read_file`. Example rows:\n"
            "    M  src/OrderService.java  (68L · +12/-8 · 2.4kB)\n"
            "    A  src/AuditLog.java      (50L · +50/-0 · 1.8kB)\n"
            "    D  src/LegacyService.java (30L · +0/-30 · 1.1kB)\n"
            "       src/Util.java          (42L · 980B)\n"
            "\n"
            "Status: `A`/`M`/`D`/`R`/`C`/`T` (added/modified/deleted/"
            "renamed/copied/type-changed) or a space for unchanged "
            "context files — same alphabet as `git diff --name-status`. "
            "Renames surface as a pair: `D <old>` + `R <new>`. "
            "Summary: `L` = total lines in the unified-diff view "
            "(= what `diff_read_file(path)` returns without a range); "
            "`+N/-M` = added/removed line counts (omitted for "
            "unchanged files, where both are zero); bytes = file size. "
            "\n\n"
            "Paginated: `start` (offset) + `n` (page size, default 50). "
            "When more rows exist than fit on the page a footer lists "
            "the total and the `start` value for the next call. "
            "`changes_only=true` (default) drops unchanged context "
            "files — call with `changes_only=false` to also see the "
            "unchanged surroundings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob, e.g. '**/*.java'. Default '**/*' = everything — call with no args to list every file."},
                "ref": {"type": "string", "description": 'Diff view: "base..source" (default in PR mode), "<sha1>..<sha2>", or "source" for plain working-tree (no markers).'},
                "changes_only": {"type": "boolean", "description": "Default true — only files with status M/A/D/R/C/T. Set false to also include unchanged context files."},
                "start": {"type": "integer", "description": "Pagination offset (default 0)."},
                "n": {"type": "integer", "description": "Page size (default 50)."},
            },
            # Nothing is required — every field has a sensible default.
            # Marking pattern as required earlier caused tool-schema
            # validation to reject the typical opening call
            # `diff_list_files()` and blocked reviewers that always
            # orient themselves with an unfiltered first listing.
            "required": [],
        },
    )
    def diff_list_files(
        pattern: str = "**/*",
        ref: str = "",
        changes_only: bool = True,
        start: int = 0,
        n: int = 50,
    ) -> str:
        _ensure()
        ref = ref or _default_ref()
        vfs_dir = _get_vfs(ctx, ref)
        if vfs_dir:
            from diffsearch.tools import list_files_vfs
            text = list_files_vfs(vfs_dir, pattern, changes_only=changes_only)
            return _filter_and_page_listing(text, start=start, n=n)
        # Plain mode (no VFS, no diff metadata) — no status info, so
        # `changes_only` is a no-op here. Paginate against the raw list.
        files = _list_files_impl(pattern, ctx.repo_path)
        files = [f for f in files if not _skip_dir(f)]
        total = len(files)
        page = files[start : start + n]
        out = _format_plain_listing(page, ctx.repo_path)
        if total > start + n or start > 0:
            out += (
                f"\n\n[showing {start}..{start + len(page) - 1} of {total} — "
                f"call with start={start + n} to see the next page]"
            )
        return out

    @registry.register(
        name="diff_read_file",
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
                "start_line": {"type": "integer", "description": "L position (1-indexed, inclusive). L = position in the unified-diff view, as shown by diff_outline."},
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
        name="diff_outline",
        description=(
            "Structural outline (classes, methods, fields) of a file in "
            "the diff view. Each symbol shows L range (use for diff_read_file "
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
        name="diff_search",
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

    # One JiraProvider per tool-registration — its network client is
    # lazy, so constructing it is free; it picks up JIRA_TOKEN or the
    # DIFFGRAPH_JIRA_FIXTURE fake-path env at call time.
    _jira_provider = None

    def _jira():
        nonlocal _jira_provider
        if _jira_provider is None:
            from .providers.jira import JiraProvider
            _jira_provider = JiraProvider()
        return _jira_provider

    @registry.register(
        name="read_ticket",
        description=(
            "Read the issue tracker ticket a PR claims to address — "
            "its summary, type, status, description / acceptance "
            "criteria, recent comments, status history, and linked "
            "issues. `ref` is copied verbatim from the PR's "
            "`jira_tickets` list (shape `handle/namespace/ticket_id`); "
            "a bare ticket key also works. Linked issues come back "
            "inline — call `read_ticket` again on a linked key to go "
            "deeper. If the ticket can't be reached (not found, Jira "
            "not configured) the tool says so plainly — proceed with "
            "the diff + PR description alone, don't retry in a loop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "Ticket reference — `handle/namespace/ticket_id` "
                        "as given in the PR's jira_tickets list, or a "
                        "bare ticket key (e.g. ORD-301)."
                    ),
                },
            },
            "required": ["ref"],
        },
    )
    def read_ticket_tool(ref: str = "") -> str:
        ref = (ref or "").strip()
        if not ref:
            return "(ref is required)"
        # Lenient parse: the ticket key is the last '/'-segment of a
        # handle/namespace/ticket_id ref, or the whole string if it's
        # a bare key. Real multi-server routing (handle → server)
        # lands with the JiraRegistry; for now the provider resolves
        # against its single configured server / the fake fixture.
        key = ref.rsplit("/", 1)[-1]
        try:
            from .providers.jira import format_ticket
            return format_ticket(_jira().fetch_ticket(key))
        except Exception as exc:  # never crash the agent on a tracker hiccup
            return (
                f"(read_ticket failed for {ref!r}: {type(exc).__name__}: "
                f"{exc} — proceed with the diff + PR description alone)"
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
        from diffgraph.bitbucket import (
            react_to_pr_comment,
            ReactionsUnsupportedError,
        )
        try:
            react_to_pr_comment(pr_url, int(comment_id), emoticon)
            return {"status": "posted",
                    "comment_id": int(comment_id),
                    "emoticon": emoticon.strip().strip(":")}
        except ReactionsUnsupportedError as exc:
            # Bitbucket Server: no public reactions API. Tell the
            # agent to use a textual reply instead of an emoji —
            # status "unsupported" so the agent doesn't retry with a
            # different emoticon hoping it'll succeed.
            return {
                "status": "unsupported",
                "comment_id": int(comment_id),
                "message": str(exc),
                "hint": (
                    "Use post_comment(parent_id=<comment_id>, text=...) "
                    "to leave a reply instead of a reaction."
                ),
            }
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
