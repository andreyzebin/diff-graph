from __future__ import annotations
import glob as _glob_mod
import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchResult:
    file: str
    line: int
    text: str
    context: list[str]  # up to 2 lines before + 2 lines after


def list_files(glob: str, repo_path: str) -> list[str]:
    """
    Find files by glob pattern. Returns relative paths.
    If >50 matches, returns first 10.
    """
    pattern = os.path.join(repo_path, glob)
    matches = _glob_mod.glob(pattern, recursive=True)
    relative = sorted(
        os.path.relpath(m, repo_path)
        for m in matches
        if os.path.isfile(m)
    )
    if len(relative) > 50:
        return relative[:10]
    return relative


def read_file(
    path: str,
    repo_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    Read a file or a line range (1-indexed, inclusive).
    Without a range: reads first 300 lines.
    """
    full_path = os.path.join(repo_path, path)
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""

    if start_line is not None and end_line is not None:
        selected = lines[start_line - 1 : end_line]
    else:
        selected = lines[:300]

    return "".join(selected)


def search_text(
    query: str,
    repo_path: str,
    glob: str = "**/*",
    regex: bool = False,
    include_context: bool = True,
) -> list[SearchResult]:
    """
    Search for a string (literal or regex) in files matching glob.

    include_context: when True (default) returns ±2 surrounding lines and full
                     matched line. When False returns only file+line+snippet
                     (first 120 chars of matched line) — useful for quick
                     relevance scanning before committing to read_file.
    """
    pattern = os.path.join(repo_path, glob)
    files = [f for f in _glob_mod.glob(pattern, recursive=True) if os.path.isfile(f)]

    if regex:
        try:
            compiled = re.compile(query)
        except re.error:
            compiled = re.compile(re.escape(query))
        match_fn = compiled.search
    else:
        match_fn = None  # plain substring check

    results: list[SearchResult] = []
    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                file_lines = f.readlines()
        except OSError:
            continue

        for i, line_content in enumerate(file_lines):
            hit = match_fn(line_content) if match_fn else (query in line_content)
            if hit:
                text = line_content.rstrip("\n")
                if include_context:
                    ctx = [
                        file_lines[j].rstrip("\n")
                        for j in range(max(0, i - 2), min(len(file_lines), i + 3))
                        if j != i
                    ]
                else:
                    ctx = []
                    text = text[:120]
                results.append(SearchResult(
                    file=os.path.relpath(filepath, repo_path),
                    line=i + 1,
                    text=text,
                    context=ctx,
                ))

    return results
