"""
Tools operating on the virtual unified diff filesystem.

read_file_vfs  — read file with old/new line number columns
search_vfs     — grep across virtual files (finds +/- content)
list_files_vfs — list files in the virtual FS
read_outline_vfs — structural outline with L ranges
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from .virtual_fs import load_diffmeta


@dataclass
class SearchHit:
    file: str
    L: int
    old: int | None
    new: int | None
    marker: str
    snippet: str


def read_file_vfs(
    vfs_dir: str,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    line_numbers: bool = True,
    changes_only: bool = False,
    context: int = 3,
    context_before: int | None = None,
    context_after: int | None = None,
) -> str:
    """
    Read a file from the virtual FS.

    start_line/end_line are L (virtual position, 1-indexed).
    Output shows old/new columns when metadata exists (changed file).
    changes_only=True shows only lines near +/- markers (±context lines).
    """
    file_path = Path(vfs_dir) / path
    if not file_path.exists():
        return "(file not found)"

    with open(file_path) as f:
        all_lines = f.readlines()

    # Binary file marker
    if len(all_lines) == 1 and all_lines[0].strip() == "(binary file)":
        return f"# {path}\n(binary file)"

    meta = load_diffmeta(vfs_dir, path)

    # changes_only: show only lines near +/- markers
    cb = context_before if context_before is not None else context
    ca = context_after if context_after is not None else context
    if changes_only and meta:
        return _render_changes_only(path, all_lines, meta, line_numbers, cb, ca)
    if changes_only and not meta:
        return f"# {path}\n(no changes)"

    if end_line is None:
        end_line = min(start_line + 99, len(all_lines))
    # Clamp
    start_line = max(1, start_line)
    end_line = min(end_line, len(all_lines))

    if not meta:
        # Unchanged file — plain output, single column
        w = len(str(end_line))
        out = [f"# {path}  L{start_line}-L{end_line}"]
        for i in range(start_line - 1, end_line):
            ln = i + 1
            content = all_lines[i].rstrip("\n")
            if line_numbers:
                out.append(f"{ln:>{w}} | {content}")
            else:
                out.append(f"  {content}")
        return "\n".join(out)

    # Changed file — detect which columns are needed
    slice_meta = meta[start_line - 1:end_line]
    has_old = any(m["old"] is not None for m in slice_meta)
    has_new = any(m["new"] is not None for m in slice_meta)
    all_plus = all(m["marker"] == "+" for m in slice_meta)
    all_minus = all(m["marker"] == "-" for m in slice_meta)

    # Dynamic column widths
    max_old = max((m["old"] for m in slice_meta if m["old"] is not None), default=0)
    max_new = max((m["new"] for m in slice_meta if m["new"] is not None), default=0)
    w_old = len(str(max_old)) if max_old else 0
    w_new = len(str(max_new)) if max_new else 0

    # Header
    if all_plus:
        tag = "(new file)"
    elif all_minus:
        tag = "(deleted)"
    else:
        tag = "(old=base, new=source)"
    out = [f"# {path}  L{start_line}-L{end_line} {tag}"]

    if line_numbers:
        cols = []
        if has_old:
            cols.append(f"{'old':>{w_old}}")
        if has_new:
            cols.append(f"{'new':>{w_new}}")
        if cols:
            out.append(" ".join(cols))

    for i in range(start_line - 1, end_line):
        if i >= len(meta):
            break
        m = meta[i]
        content = all_lines[i].rstrip("\n")
        marker = m["marker"] if m["marker"] != " " else " "

        if line_numbers:
            parts = []
            if has_old:
                parts.append(f"{m['old']:>{w_old}}" if m["old"] is not None else " " * w_old)
            if has_new:
                parts.append(f"{m['new']:>{w_new}}" if m["new"] is not None else " " * w_new)
            out.append(f"{' '.join(parts)} |{marker} {content}")
        else:
            out.append(f"|{marker} {content}")

    return "\n".join(out)


def _render_changes_only(
    path: str, all_lines: list[str], meta: list[dict],
    line_numbers: bool, before: int, after: int,
) -> str:
    """Render only lines near +/- markers with before/after context."""
    # Find L positions of changed lines
    changed_Ls: set[int] = set()
    for m in meta:
        if m["marker"] in ("+", "-"):
            changed_Ls.add(m["L"])

    if not changed_Ls:
        return f"# {path}\n(no changes)"

    # Expand with context
    show_Ls: set[int] = set()
    for L in changed_Ls:
        for offset in range(-before, after + 1):
            candidate = L + offset
            if 1 <= candidate <= len(meta):
                show_Ls.add(candidate)

    # Detect columns needed
    show_meta = [meta[L - 1] for L in sorted(show_Ls)]
    has_old = any(m["old"] is not None for m in show_meta)
    has_new = any(m["new"] is not None for m in show_meta)

    max_old = max((m["old"] for m in show_meta if m["old"] is not None), default=0)
    max_new = max((m["new"] for m in show_meta if m["new"] is not None), default=0)
    w_old = len(str(max_old)) if max_old else 0
    w_new = len(str(max_new)) if max_new else 0

    all_plus = all(m["marker"] == "+" for m in meta)
    all_minus = all(m["marker"] == "-" for m in meta)
    if all_plus:
        tag = "(new file)"
    elif all_minus:
        tag = "(deleted)"
    else:
        tag = "(old=base, new=source)"

    out = [f"# {path}  changes only {tag}"]

    if line_numbers:
        cols = []
        if has_old:
            cols.append(f"{'old':>{w_old}}")
        if has_new:
            cols.append(f"{'new':>{w_new}}")
        if cols:
            out.append(" ".join(cols))

    prev_L = 0
    for L in sorted(show_Ls):
        if prev_L and L > prev_L + 1:
            out.append("  --")

        m = meta[L - 1]
        content = all_lines[L - 1].rstrip("\n") if L <= len(all_lines) else ""
        marker = m["marker"] if m["marker"] != " " else " "

        if line_numbers:
            parts = []
            if has_old:
                parts.append(f"{m['old']:>{w_old}}" if m["old"] is not None else " " * w_old)
            if has_new:
                parts.append(f"{m['new']:>{w_new}}" if m["new"] is not None else " " * w_new)
            out.append(f"{' '.join(parts)} |{marker} {content}")
        else:
            out.append(f"|{marker} {content}")

        prev_L = L

    return "\n".join(out)


def search_vfs(
    vfs_dir: str,
    query: str,
    glob: str = "**/*",
    regex: bool = False,
    max_results: int = 30,
    before: int = 0,
    after: int = 0,
) -> str:
    """
    Search across virtual FS files.

    Returns grep-like text grouped by file with old/new coordinates.
    Uses grep on materialized files, enriches with .diffmeta/.
    """
    vfs = Path(vfs_dir)

    # Use grep on the vfs directory.
    # `-E` (extended regex) when regex=True — plain `grep` defaults to BRE,
    # where `|`/`+`/`?`/`()` are literal. Without `-E` a query like
    # `store.credit|storeCredit|StoreCredit` matches zero lines because
    # the `|` is treated as a literal pipe — a silent zero-result bug
    # that looks like "nothing found" to the agent.
    cmd = ["grep", "-rni"]
    if regex:
        cmd.append("-E")  # extended regex (alternation, +, ?, (), {})
    else:
        cmd.append("-F")  # fixed string
    # --include with grep uses shell glob (not **), so convert
    if glob and glob != "**/*":
        cmd.extend(["--include", glob.replace("**/", "")])
    cmd.extend([query, str(vfs)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):  # 1 = no matches
        return "(no matches)"

    # Parse grep output into raw hits
    raw_hits: list[SearchHit] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        match = re.match(r"^(.+?):(\d+):(.*)$", line)
        if not match:
            continue

        abs_path = match.group(1)
        line_num = int(match.group(2))
        snippet = match.group(3)

        try:
            rel_path = str(Path(abs_path).relative_to(vfs))
        except ValueError:
            continue

        if rel_path.startswith(".diffmeta"):
            continue
        if snippet.strip() == "(binary file)":
            continue

        meta = load_diffmeta(vfs_dir, rel_path)
        if meta and line_num <= len(meta):
            m = meta[line_num - 1]
            raw_hits.append(SearchHit(
                file=rel_path, L=line_num,
                old=m["old"], new=m["new"],
                marker=m["marker"], snippet=snippet,
            ))
        else:
            raw_hits.append(SearchHit(
                file=rel_path, L=line_num,
                old=line_num, new=line_num,
                marker=" ", snippet=snippet,
            ))

        if len(raw_hits) >= max_results:
            break

    if not raw_hits:
        return "(no matches)"

    # Format output: grouped by file, with context lines
    return _format_search_results(vfs_dir, raw_hits, before, after)


def _format_search_results(
    vfs_dir: str, hits: list[SearchHit], before: int, after: int,
) -> str:
    """Format search hits as grep-like text grouped by file."""
    # Group by file
    from collections import OrderedDict
    grouped: OrderedDict[str, list[SearchHit]] = OrderedDict()
    for h in hits:
        grouped.setdefault(h.file, []).append(h)

    parts: list[str] = []
    for file_path, file_hits in grouped.items():
        parts.append(file_path)

        # Load file lines + meta for context
        file_full = Path(vfs_dir) / file_path
        meta = load_diffmeta(vfs_dir, file_path)
        try:
            all_lines = file_full.read_text().splitlines()
        except Exception:
            all_lines = []

        # Collect all line numbers to show (hits + context)
        hit_lines = {h.L for h in file_hits}
        show_lines: set[int] = set()
        for L in hit_lines:
            for offset in range(-before, after + 1):
                candidate = L + offset
                if 1 <= candidate <= len(all_lines):
                    show_lines.add(candidate)

        # Render lines in order with separators between groups
        prev_L = 0
        for L in sorted(show_lines):
            if prev_L and L > prev_L + 1:
                parts.append("  --")

            content = all_lines[L - 1] if L <= len(all_lines) else ""

            if meta and L <= len(meta):
                m = meta[L - 1]
                old_s = f"old:{m['old']}" if m["old"] is not None else ""
                new_s = f"new:{m['new']}" if m["new"] is not None else ""
                marker = m["marker"]
            else:
                old_s = f"old:{L}"
                new_s = f"new:{L}"
                marker = " "

            coords = f"L{L} {old_s} {new_s}".rstrip()
            marker_char = marker if marker != " " else " "
            parts.append(f"  {coords} |{marker_char} {content}")

            prev_L = L

        parts.append("")  # blank line between files

    return "\n".join(parts).rstrip()


def _fmt_bytes(n: int) -> str:
    """Compact byte size for diff_list_files summaries — `980B`, `2.4kB`,
    `1.2MB`. We intentionally stop at MB: VFS files larger than that are
    almost always generated/binary and read by the agent at most once."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}kB"
    return f"{n/(1024*1024):.1f}MB"


def list_files_vfs(
    vfs_dir: str,
    glob_pattern: str = "**/*",
    *,
    changes_only: bool = False,
) -> str:
    """List files in the virtual FS as plain text, one row per file:

        M  src/OrderService.java  (68L · +12/-8 · 2.4kB)
        A  src/AuditLog.java      (50L · +50/-0 · 1.8kB)
        D  src/LegacyService.java (30L · +0/-30 · 1.1kB)
        R  src/OrderUtils.java    (25L · +0/-0 · 720B)
           src/Util.java          (42L · 980B)

    The leading status code mirrors the `+` / `-` / ` ` line markers
    in diff_read_file / diff_search — A/M/D/R/C/T from git diff plus a
    space for unchanged context files. Renames surface as a `D <old>`
    + `R <new>` pair so both paths exist in the listing.

    The trailing `(...)` summary lets the agent estimate the cost of
    reading the file *before* calling diff_read_file:

      - `L`     — total lines in the unified-diff view (== what you'd
                  get from `diff_read_file(path)` without a range).
      - `+N/-M` — added / removed line counts. Big delta vs small `L`
                  means `changes_only=true` saves little; small delta
                  vs big `L` means it saves a lot.
      - bytes   — file size on disk (the materialised VFS file holds
                  the unified-diff content for changed files, so this
                  is the byte cost of a full read).

    For unchanged files (status=` `) we drop `+N/-M` (always zero by
    definition) and show only `L · bytes`. Binary files are listed as
    `(binary)` — no useful L / +N/-M to report.

    The `.diffmeta/` directory is hidden. Returns an empty string when
    no files match the glob."""
    from .virtual_fs import load_path_status
    vfs = Path(vfs_dir)
    statuses = load_path_status(vfs_dir)
    paths = sorted(vfs.glob(glob_pattern))

    rows: list[tuple[str, str, str]] = []  # (status, path, summary)
    for p in paths:
        if not p.is_file():
            continue
        rel = str(p.relative_to(vfs))
        if rel.startswith(".diffmeta"):
            continue
        st = statuses.get(rel, " ")
        # `changes_only=true` drops unchanged context files (status=` `).
        # They're often the bulk of the listing on big repos with small
        # PRs — without this filter the meaningful M/A/D rows can fall
        # past any downstream truncation cap.
        if changes_only and st == " ":
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0

        # Binary marker file — `(binary file)\n` written by
        # materialize_vfs. No meaningful line counts.
        is_binary = False
        try:
            if size <= 32:
                head = p.read_text()
                if head.strip() == "(binary file)":
                    is_binary = True
        except (UnicodeDecodeError, OSError):
            is_binary = True

        if is_binary:
            rows.append((st, rel, "(binary)"))
            continue

        meta = load_diffmeta(vfs_dir, rel)
        if meta is not None:
            # Changed file — L from meta length, +/- from markers.
            L = len(meta)
            plus = sum(1 for m in meta if m.get("marker") == "+")
            minus = sum(1 for m in meta if m.get("marker") == "-")
            summary = f"({L}L · +{plus}/-{minus} · {_fmt_bytes(size)})"
        else:
            # Unchanged file — count lines from content (cheap; agent
            # rarely lists tens of thousands of unchanged files).
            try:
                with open(p, "rb") as f:
                    L = sum(1 for _ in f)
            except OSError:
                L = 0
            summary = f"({L}L · {_fmt_bytes(size)})"
        rows.append((st, rel, summary))

    if not rows:
        return ""

    # Column-align path → summary so the `(` lines up. The status
    # prefix is always `<X>  ` (status char, two spaces), 3 cols wide,
    # so unchanged rows (status=' ') and changed rows align without
    # extra logic.
    path_w = max(len(r[1]) for r in rows)
    out = [f"{st}  {path:<{path_w}}  {summary}"
           for st, path, summary in rows]
    return "\n".join(out)


def read_outline_vfs(
    vfs_dir: str,
    path: str,
    repo_path: Optional[str] = None,
) -> str:
    """
    Structural outline of a file in the virtual FS.

    For changed files: two-pass parsing (blank +/- lines), merged with Lold/Lnew.
    For unchanged files: single-pass, plain L numbers.
    """
    from .outline import parse_outline_vfs as _parse
    meta = load_diffmeta(vfs_dir, path)
    return _parse(vfs_dir, path, meta)
