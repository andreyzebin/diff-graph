"""
DiffSearch — virtual unified diff filesystem.

Materializes a git diff as a virtual filesystem where changed files
contain both old and new content with +/- markers, and unchanged files
are symlinked to the source version.
"""
from .virtual_fs import (
    VirtualLine,
    VirtualFile,
    build_virtual_file,
    materialize_vfs,
    get_changed_files,
)
from .tools import read_file_vfs, search_vfs, list_files_vfs, read_outline_vfs
