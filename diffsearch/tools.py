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
) -> str:
    """
    Read a file from the virtual FS.

    start_line/end_line are L (virtual position, 1-indexed).
    Output shows old/new columns when metadata exists (changed file).
    """
    file_path = Path(vfs_dir) / path
    if not file_path.exists():
        return "(file not found)"

    with open(file_path) as f:
        all_lines = f.readlines()

    meta = load_diffmeta(vfs_dir, path)

    if end_line is None:
        end_line = min(start_line + 99, len(all_lines))
    # Clamp
    start_line = max(1, start_line)
    end_line = min(end_line, len(all_lines))

    if not meta:
        # Unchanged file — plain output
        out = []
        if line_numbers:
            header = f"# {path}  lines {start_line}-{end_line}\n"
        else:
            header = f"# {path}\n"
        out.append(header)
        for i in range(start_line - 1, end_line):
            ln = i + 1
            content = all_lines[i].rstrip("\n")
            if line_numbers:
                out.append(f"  {ln:>4} | {content}")
            else:
                out.append(f"  {content}")
        return "\n".join(out)

    # Changed file — show old/new columns + markers
    out = []
    header = f"# {path}  lines L{start_line}-L{end_line}  (old=left commit, new=right commit)"
    out.append(header)
    if line_numbers:
        out.append("   old  new")

    for i in range(start_line - 1, end_line):
        if i >= len(meta):
            break
        m = meta[i]
        content = all_lines[i].rstrip("\n")
        marker = m["marker"]
        marker_char = marker if marker != " " else " "

        if line_numbers:
            old_s = f"{m['old']:>5}" if m["old"] is not None else "     "
            new_s = f"{m['new']:>5}" if m["new"] is not None else "     "
            out.append(f"  {old_s}{new_s} |{marker_char}{content}")
        else:
            out.append(f"  {marker_char}{content}")

    return "\n".join(out)


def search_vfs(
    vfs_dir: str,
    query: str,
    glob: str = "**/*",
    regex: bool = False,
    max_results: int = 30,
) -> list[SearchHit]:
    """
    Search across virtual FS files.

    Uses grep on materialized files. Enriches results with old/new
    from .diffmeta/ for changed files.
    """
    vfs = Path(vfs_dir)

    # Use grep on the vfs directory
    cmd = ["grep", "-rn"]
    if not regex:
        cmd.append("-F")  # fixed string
    # --include with grep uses shell glob (not **), so convert
    if glob and glob != "**/*":
        cmd.extend(["--include", glob.replace("**/", "")])
    cmd.extend([query, str(vfs)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):  # 1 = no matches
        return []

    hits: list[SearchHit] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        # Format: /path/to/vfs/dir/src/File.java:42:content
        match = re.match(r"^(.+?):(\d+):(.*)$", line)
        if not match:
            continue

        abs_path = match.group(1)
        line_num = int(match.group(2))
        snippet = match.group(3)

        # Convert to relative path
        try:
            rel_path = str(Path(abs_path).relative_to(vfs))
        except ValueError:
            continue

        # Skip .diffmeta files
        if rel_path.startswith(".diffmeta"):
            continue

        # Enrich with old/new from metadata
        meta = load_diffmeta(vfs_dir, rel_path)
        if meta and line_num <= len(meta):
            m = meta[line_num - 1]
            hits.append(SearchHit(
                file=rel_path, L=line_num,
                old=m["old"], new=m["new"],
                marker=m["marker"], snippet=snippet,
            ))
        else:
            # Unchanged file: L == old == new
            hits.append(SearchHit(
                file=rel_path, L=line_num,
                old=line_num, new=line_num,
                marker=" ", snippet=snippet,
            ))

        if len(hits) >= max_results:
            break

    return hits


def list_files_vfs(vfs_dir: str, glob_pattern: str = "**/*") -> list[str]:
    """List files in the virtual FS, excluding .diffmeta/."""
    vfs = Path(vfs_dir)
    paths = sorted(vfs.glob(glob_pattern))
    result = []
    for p in paths:
        if p.is_file():
            rel = str(p.relative_to(vfs))
            if not rel.startswith(".diffmeta"):
                result.append(rel)
    return result


def read_outline_vfs(
    vfs_dir: str,
    path: str,
    repo_path: Optional[str] = None,
) -> str:
    """
    Structural outline of a file in the virtual FS.

    For changed files: shows L ranges + old/new mapping.
    For unchanged files: shows plain line numbers (L == old == new).

    Uses tree-sitter if available, falls back to line count.
    """
    file_path = Path(vfs_dir) / path
    if not file_path.exists():
        return f"(file not found: {path})"

    meta = load_diffmeta(vfs_dir, path)

    # Try tree-sitter outline
    try:
        from diffgraph.outline import get_outline as _ts_outline
        # Read the virtual file (contains both old+new content for changed files)
        outline_text = _ts_outline(
            path=str(file_path),
            repo_path=vfs_dir,
            changed_lines=None,  # we'll annotate ourselves
        )
    except (ImportError, Exception):
        # Fallback: line count
        with open(file_path) as f:
            total = sum(1 for _ in f)
        return f"# {path}  ({total} lines)\n(tree-sitter unavailable)"

    if not meta:
        # Unchanged file — return plain outline
        return outline_text

    # Enrich: replace "L10-50" with "L10-50 (old:10-50 → new:10-50)"
    # and mark changed methods with *
    changed_Ls = {m["L"] for m in meta if m["marker"] != " "}
    enriched_lines = []

    for line in outline_text.splitlines():
        # Match outline entries like "[method] name  L10-50" or "[class] name  L10-50"
        m = re.match(r"^(\s*\[.+?\]\s+\S+\s+)L(\d+)-(\d+)(.*)", line)
        if m:
            prefix = m.group(1)
            l_start = int(m.group(2))
            l_end = int(m.group(3))
            suffix = m.group(4)

            # Look up old/new ranges from metadata
            old_start = _L_to_linenum(meta, l_start, "old")
            old_end = _L_to_linenum(meta, l_end, "old")
            new_start = _L_to_linenum(meta, l_start, "new")
            new_end = _L_to_linenum(meta, l_end, "new")

            old_range = f"old:{old_start}-{old_end}" if old_start and old_end else ("deleted" if not new_start else "")
            new_range = f"new:{new_start}-{new_end}" if new_start and new_end else ("added" if not old_start else "")

            # Check if any L in range is changed
            is_changed = any(L in changed_Ls for L in range(l_start, l_end + 1))
            star = " *" if is_changed else ""

            enriched_lines.append(
                f"{prefix}L{l_start}-{l_end} ({old_range} → {new_range}){star}{suffix}"
            )
        else:
            enriched_lines.append(line)

    return "\n".join(enriched_lines)


def _L_to_linenum(meta: list[dict], L: int, which: str) -> int | None:
    """Get old or new line number for a given L position."""
    if L < 1 or L > len(meta):
        return None
    return meta[L - 1].get(which)
