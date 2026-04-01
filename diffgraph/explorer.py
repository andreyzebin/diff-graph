from __future__ import annotations
import logging
from collections import deque
from typing import Optional

from .extractor import extract_module
from .lang import detect_lang, get_extensions, get_search_patterns
from .model import MetaModel
from .tools import list_files, read_file, search_text

log = logging.getLogger(__name__)


def explore(
    start_files: list[str],
    repo_path: str,
    llm,
    model: str = "gpt-4o-mini",
    max_depth: int = 2,
) -> MetaModel:
    """
    BFS over dependencies starting from start_files.
    Reads after-versions of files from repo_path.
    """
    meta = MetaModel()
    queue: deque[tuple[str, int]] = deque((f, 0) for f in start_files)
    visited: set[str] = set()

    while queue:
        file_path, depth = queue.popleft()
        if file_path in visited or depth > max_depth:
            continue
        visited.add(file_path)

        content = read_file(file_path, repo_path)
        if not content:
            log.warning("explore: could not read %s, skipping", file_path)
            continue

        lang = detect_lang(file_path)
        module = extract_module(file_path, content, lang, llm, model)
        if module is None:
            continue

        module.depth = depth
        meta.add(module)

        if depth < max_depth:
            for dep_name in module.dependencies:
                dep_file = resolve_dependency(dep_name, lang, repo_path)
                if dep_file and dep_file not in visited:
                    queue.append((dep_file, depth + 1))

    return meta


def resolve_dependency(name: str, lang: str, repo_path: str) -> Optional[str]:
    """
    Find the file that defines a dependency by name.

    Step 1: Direct file lookup by name + extension.
    Step 2: Text search for class/interface declaration.
    Step 3: Return None (external library / stdlib).
    """
    # Step 1: file by name
    for ext in get_extensions(lang):
        files = list_files(f"**/{name}{ext}", repo_path)
        if files:
            return _best_match(files, name)

    # Step 2: declaration search
    for pattern in get_search_patterns(name, lang):
        results = search_text(pattern, repo_path)
        if results:
            return results[0].file

    return None


def _best_match(files: list[str], dep_name: str) -> str:
    """
    Pick the best file from a list of candidates.
    Prefers the shortest path (closest to repo root).
    Warns if >10 candidates.
    """
    if len(files) == 1:
        return files[0]
    if len(files) > 10:
        log.warning("_best_match: >10 candidates for '%s', using top 3", dep_name)
        files = files[:3]
    return min(files, key=lambda p: len(p))
