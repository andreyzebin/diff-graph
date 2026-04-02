from __future__ import annotations
import logging
from collections import deque
from typing import Callable, Optional

from .extractor import OnEvent, extract_module
from .impact_agent import ImpactHit, find_impact
from .lang import detect_lang, get_extensions, get_globs_for_lang, get_search_patterns
from .model import MetaModel
from .tools import list_files, read_file, search_text

log = logging.getLogger(__name__)


def explore(
    start_files: list[str],
    repo_path: str,
    llm,
    model: str = "gpt-4o-mini",
    max_depth: int = 2,
    on_event: Optional[OnEvent] = None,
) -> MetaModel:
    """
    BFS over dependencies starting from start_files.
    Reads after-versions of files from repo_path.
    """
    _emit = on_event or (lambda *_, **__: None)
    meta = MetaModel()
    queue: deque[tuple[str, int]] = deque((f, 0) for f in start_files)
    visited: set[str] = set()

    while queue:
        file_path, depth = queue.popleft()
        if file_path in visited or depth > max_depth:
            continue
        visited.add(file_path)

        _emit("reading", path=file_path, depth=depth)
        content = read_file(file_path, repo_path)
        if not content:
            log.warning("explore: could not read %s, skipping", file_path)
            _emit("read_failed", path=file_path)
            continue

        lang = detect_lang(file_path)
        module = extract_module(file_path, content, lang, llm, model, on_event=on_event)
        if module is None:
            continue

        module.depth = depth
        meta.add(module)

        if depth < max_depth:
            for dep_name in module.dependencies:
                _emit("resolving", name=dep_name)
                dep_file = resolve_dependency(dep_name, lang, repo_path)
                if dep_file:
                    _emit("resolved", name=dep_name, path=dep_file)
                    if dep_file not in visited:
                        queue.append((dep_file, depth + 1))
                else:
                    _emit("not_resolved", name=dep_name)

    return meta


def explore_callers(
    model: MetaModel,
    repo_path: str,
    llm,
    llm_model: str = "gpt-4o-mini",
    max_callers: int = 5,
    exclude_tests: bool = True,
    max_agent_steps: int = 12,
    max_agent_tokens: int = 20000,
    on_event: Optional[OnEvent] = None,
) -> None:
    """
    Agentic impact analysis: for each changed module, run a ReAct agent that
    uses list_files / search / read_file to find files impacted by the change.

    High- and medium-confidence hits are extracted and added to the MetaModel
    with depth=-1 (callers / impacted files).
    """
    _emit = on_event or (lambda *_, **__: None)
    visited = set(model.modules.keys())

    for module_id in list(model.changed_module_ids):
        module = model.modules[module_id]

        # Skip if no symbols were actually marked changed
        if not any(s.is_changed for s in module.symbols):
            continue

        _emit("searching_callers", name=module.name, path=module_id)

        hits = find_impact(
            module=module,
            repo_path=repo_path,
            llm=llm,
            model=llm_model,
            max_steps=max_agent_steps,
            max_tokens=max_agent_tokens,
            on_event=on_event,
        )

        count = 0
        for hit in hits:
            if hit.confidence == "low":
                continue
            if exclude_tests and _is_test_file(hit.file):
                continue
            if hit.file in visited:
                continue
            if count >= max_callers:
                break

            visited.add(hit.file)
            content = read_file(hit.file, repo_path)
            if not content:
                continue

            lang = detect_lang(hit.file)
            caller_mod = extract_module(hit.file, content, lang, llm, llm_model, on_event=on_event)
            if caller_mod is None:
                continue

            caller_mod.depth = -1
            model.add(caller_mod)
            model.caller_module_ids.append(hit.file)
            _emit("caller_found", path=hit.file, referenced=module.name, reason=hit.reason, confidence=hit.confidence)
            count += 1


def _is_test_file(path: str) -> bool:
    lower = path.lower()
    return (
        "/test" in lower
        or "/tests/" in lower
        or lower.endswith("_test.py")
        or lower.endswith("_test.go")
        or lower.endswith("test.java")
        or lower.endswith("spec.ts")
        or lower.endswith("spec.js")
    )


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
