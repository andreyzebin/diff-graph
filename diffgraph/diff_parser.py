from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HunkSnippet:
    """One contiguous change block within a file."""
    before_lines: list[str]   # removed lines (without '-' prefix)
    after_lines: list[str]    # added lines (without '+' prefix)
    before_start: int         # starting line in the before version
    after_start: int          # starting line in the after version
    deletion_positions: list[int] = field(default_factory=list)
    # after-line number where each '-' line was removed (after_no at time of deletion).
    # Used to detect pure-deletion changes inside a symbol.


@dataclass
class FileDiff:
    """All changes for one file."""
    path: str                       # after-version path
    before_path: Optional[str]      # path before rename (None for new files)
    status: str                     # "modified" | "added" | "deleted" | "renamed"
    hunks: list[HunkSnippet]
    after_changed_lines: list[int]  # line numbers of '+' lines in the after version


@dataclass
class DiffResult:
    files: dict[str, FileDiff]           # path → FileDiff
    changed_files: list[str]             # after-paths for BFS (excludes deleted)
    changed_lines: dict[str, list[int]]  # path → after_changed_lines


# ── public ──────────────────────────────────────────────────────────────────

def parse_diff(diff_text: str) -> DiffResult:
    """
    Parse raw git diff text into a DiffResult.
    Pure regex, no external libraries.
    """
    files: dict[str, FileDiff] = {}

    # Split on each "diff --git" header (keep the delimiter via lookahead)
    sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)

    for section in sections:
        if not section.startswith("diff --git"):
            continue
        file_diff = _parse_file_section(section)
        if file_diff is not None:
            files[file_diff.path] = file_diff

    changed_files = [fd.path for fd in files.values() if fd.status != "deleted"]
    changed_lines = {
        fd.path: fd.after_changed_lines
        for fd in files.values()
        if fd.after_changed_lines
    }

    return DiffResult(
        files=files,
        changed_files=changed_files,
        changed_lines=changed_lines,
    )


# ── internals ───────────────────────────────────────────────────────────────

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_file_section(section: str) -> Optional[FileDiff]:
    lines = section.splitlines()
    if not lines:
        return None

    # Parse "diff --git a/X b/Y" header
    header_m = re.match(r"^diff --git a/(.*) b/(.*)$", lines[0])
    if not header_m:
        return None

    before_path: Optional[str] = header_m.group(1)
    after_path: str = header_m.group(2)
    status = "modified"

    # Scan the meta-header lines (before the first @@) for status markers
    for line in lines[1:]:
        if _HUNK_HEADER.match(line):
            break
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename from") or line.startswith("similarity index"):
            status = "renamed"
        elif line.startswith("+++ b/"):
            after_path = line[6:]
        elif line == "+++ /dev/null":
            # deleted file — after path stays as parsed from header
            pass
        elif line.startswith("--- a/"):
            before_path = line[6:]
        elif line == "--- /dev/null":
            before_path = None

    hunks: list[HunkSnippet] = []
    after_changed_lines_set: set[int] = set()

    i = 0
    while i < len(lines):
        hunk_m = _HUNK_HEADER.match(lines[i])
        if hunk_m:
            before_start = int(hunk_m.group(1))
            after_start = int(hunk_m.group(2))
            before_lines: list[str] = []
            after_lines_buf: list[str] = []
            deletion_positions_buf: list[int] = []
            before_no = before_start
            after_no = after_start
            i += 1

            while i < len(lines):
                hl = lines[i]
                if _HUNK_HEADER.match(hl) or hl.startswith("diff --git"):
                    break
                if hl.startswith("-"):
                    before_lines.append(hl[1:])
                    if after_no > 0:  # after_no=0 means deleted file — skip
                        deletion_positions_buf.append(after_no)
                        after_changed_lines_set.add(after_no)
                    before_no += 1
                elif hl.startswith("+"):
                    after_lines_buf.append(hl[1:])
                    after_changed_lines_set.add(after_no)
                    after_no += 1
                elif hl.startswith("\\"):
                    pass  # "No newline at end of file"
                else:
                    # context line
                    before_no += 1
                    after_no += 1
                i += 1

            hunks.append(HunkSnippet(
                before_lines=before_lines,
                after_lines=after_lines_buf,
                before_start=before_start,
                after_start=after_start,
                deletion_positions=deletion_positions_buf,
            ))
        else:
            i += 1

    return FileDiff(
        path=after_path,
        before_path=before_path,
        status=status,
        hunks=hunks,
        after_changed_lines=sorted(after_changed_lines_set),
    )
