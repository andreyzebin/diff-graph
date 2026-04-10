"""
Virtual unified diff filesystem.

Builds virtual files from git diff -U99999 output where each line
carries a marker (+/-/space) and both old and new line numbers.
Materializes to a temp directory for grep/search compatibility.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class VirtualLine:
    content: str        # line text (no +/- prefix)
    marker: str         # "+", "-", or " "
    L: int              # virtual position (1-indexed, always present)
    old: int | None     # left-commit line number (None for + lines)
    new: int | None     # right-commit line number (None for - lines)


@dataclass
class VirtualFile:
    path: str
    lines: list[VirtualLine]
    L_to_new: dict[int, int] = field(default_factory=dict)
    new_to_L: dict[int, int] = field(default_factory=dict)
    L_to_old: dict[int, int] = field(default_factory=dict)
    old_to_L: dict[int, int] = field(default_factory=dict)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def get_changed_files(base_ref: str, source_ref: str, repo_path: str) -> list[str]:
    """Return list of files changed between two refs."""
    out = subprocess.run(
        ["git", "diff", "--name-only", base_ref, source_ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return [f for f in out.stdout.strip().splitlines() if f]


def build_virtual_file(
    base_ref: str, source_ref: str, path: str, repo_path: str
) -> VirtualFile:
    """Build a VirtualFile from full-context unified diff."""
    result = subprocess.run(
        ["git", "diff", "-U99999", base_ref, source_ref, "--", path],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    diff_output = result.stdout

    if not diff_output.strip():
        # No diff — file unchanged or identical. Read source version as-is.
        return _build_plain_file(source_ref, path, repo_path)

    lines: list[VirtualLine] = []
    L = 0
    old_n = 0
    new_n = 0
    in_hunk = False

    for raw_line in diff_output.splitlines():
        # Skip diff headers
        if raw_line.startswith("diff --git"):
            continue
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("index ") or raw_line.startswith("Binary "):
            continue
        if raw_line.startswith("new file") or raw_line.startswith("deleted file"):
            continue
        if raw_line.startswith("old mode") or raw_line.startswith("new mode"):
            continue

        m = _HUNK_RE.match(raw_line)
        if m:
            old_n = int(m.group(1)) - 1  # will increment on first line
            new_n = int(m.group(2)) - 1
            in_hunk = True
            continue

        if not in_hunk:
            continue

        # Determine marker and content
        if raw_line.startswith("-"):
            marker = "-"
            content = raw_line[1:]
            old_n += 1
            L += 1
            lines.append(VirtualLine(content, marker, L, old=old_n, new=None))
        elif raw_line.startswith("+"):
            marker = "+"
            content = raw_line[1:]
            new_n += 1
            L += 1
            lines.append(VirtualLine(content, marker, L, old=None, new=new_n))
        else:
            # Context line (starts with space or is empty)
            marker = " "
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            old_n += 1
            new_n += 1
            L += 1
            lines.append(VirtualLine(content, marker, L, old=old_n, new=new_n))

    vf = VirtualFile(path=path, lines=lines)
    _build_mappings(vf)
    return vf


def _build_plain_file(ref: str, path: str, repo_path: str) -> VirtualFile:
    """Build VirtualFile for an unchanged file: L == old == new."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_path, capture_output=True,
    )
    if result.returncode != 0:
        return VirtualFile(path=path, lines=[])
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return VirtualFile(path=path, lines=[])  # binary file
    lines = []
    for i, content in enumerate(text.splitlines(), 1):
        lines.append(VirtualLine(content, " ", L=i, old=i, new=i))
    vf = VirtualFile(path=path, lines=lines)
    _build_mappings(vf)
    return vf


def _build_mappings(vf: VirtualFile) -> None:
    """Populate L↔old and L↔new dictionaries."""
    vf.L_to_new = {}
    vf.new_to_L = {}
    vf.L_to_old = {}
    vf.old_to_L = {}
    for line in vf.lines:
        if line.new is not None:
            vf.L_to_new[line.L] = line.new
            vf.new_to_L[line.new] = line.L
        if line.old is not None:
            vf.L_to_old[line.L] = line.old
            vf.old_to_L[line.old] = line.L


def materialize_vfs(
    repo_path: str,
    base_ref: str,
    source_ref: str,
    target_dir: Optional[str] = None,
) -> str:
    """
    Materialize virtual FS to a directory.

    Changed files: written as plain text (content without markers).
    Unchanged files: symlinked to originals in repo.
    Metadata: .diffmeta/<path>.json with line mappings.

    Returns the target directory path.
    """
    if target_dir is None:
        import tempfile
        target_dir = tempfile.mkdtemp(prefix="diffsearch-vfs-")

    changed = set(get_changed_files(base_ref, source_ref, repo_path))

    # Get all files in source ref
    all_files_out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", source_ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    all_files = [f for f in all_files_out.stdout.strip().splitlines() if f]

    # Also get files only in base (deleted files)
    base_files_out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    base_only = set(base_files_out.stdout.strip().splitlines()) - set(all_files)

    virtual_files: dict[str, VirtualFile] = {}

    # Changed files: build virtual, write content, save metadata
    for path in changed:
        vf = build_virtual_file(base_ref, source_ref, path, repo_path)
        if not vf.lines:
            continue  # binary or empty file
        virtual_files[path] = vf

        # Write content (without markers) — line numbers in this file == L
        out_path = Path(target_dir) / path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            for line in vf.lines:
                f.write(line.content + "\n")

        # Write metadata
        meta_path = Path(target_dir) / ".diffmeta" / (path + ".json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = [
            {"L": l.L, "marker": l.marker, "old": l.old, "new": l.new}
            for l in vf.lines
        ]
        with open(meta_path, "w") as f:
            json.dump(meta, f)

    # Deleted files (only in base): also build virtual, all lines are "-"
    for path in base_only:
        if path in changed:
            continue
        vf = build_virtual_file(base_ref, source_ref, path, repo_path)
        if vf.lines:
            virtual_files[path] = vf
            out_path = Path(target_dir) / path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                for line in vf.lines:
                    f.write(line.content + "\n")
            meta_path = Path(target_dir) / ".diffmeta" / (path + ".json")
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta = [
                {"L": l.L, "marker": l.marker, "old": l.old, "new": l.new}
                for l in vf.lines
            ]
            with open(meta_path, "w") as f:
                json.dump(meta, f)

    # Unchanged files: write from git
    for path in all_files:
        if path in changed:
            continue
        out_path = Path(target_dir) / path
        if out_path.exists():
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "show", f"{source_ref}:{path}"],
            cwd=repo_path, capture_output=True,
        )
        if result.returncode == 0:
            # Skip binary files
            try:
                content = result.stdout.decode("utf-8")
                with open(out_path, "w") as f:
                    f.write(content)
            except UnicodeDecodeError:
                pass  # binary file, skip

    return target_dir


def load_diffmeta(vfs_dir: str, path: str) -> list[dict] | None:
    """Load metadata for a file from .diffmeta/ directory."""
    meta_path = Path(vfs_dir) / ".diffmeta" / (path + ".json")
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        return json.load(f)
