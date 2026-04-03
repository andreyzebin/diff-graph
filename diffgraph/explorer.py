from __future__ import annotations
import logging
from collections import deque
from typing import Optional

from .agents.extractor import OnEvent, extract_module
from .agents.impact import ImpactHit, find_impact
from .diff_parser import DiffResult
from .lang import detect_lang, get_globs_for_lang
from .model import MetaModel
from .agents.resolver import is_likely_external, resolve_dep
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
    Non-source files are skipped. Each dependency is resolved via the
    resolver agent to find its source file path.
    """
    _emit = on_event or (lambda *_, **__: None)
    meta = MetaModel()

    source_files = [f for f in start_files if detect_lang(f) != "unknown"]
    for f in set(start_files) - set(source_files):
        _emit("skipped", path=f, reason="not a source file")

    queue: deque[tuple[str, int]] = deque((f, 0) for f in source_files)
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

        meta.add(module)

        if depth < max_depth:
            for dep in module.dependencies:
                # Fast pre-filter for known external libraries
                if is_likely_external(dep.fqn or dep.name):
                    _emit("not_resolved", name=dep.name)
                    continue

                resolved_path = resolve_dep(
                    dep, module.name, repo_path, llm, model, on_event=on_event,
                )
                if resolved_path:
                    dep.file_path = resolved_path
                    if resolved_path not in visited:
                        queue.append((resolved_path, depth + 1))

    return meta


def explore_callers(
    model: MetaModel,
    repo_path: str,
    llm,
    llm_model: str = "gpt-4o-mini",
    max_callers: int = 5,
    exclude_tests: bool = True,
    max_agent_steps: int = 32,
    max_agent_tokens: int = 20000,
    diff_result: Optional[DiffResult] = None,
    on_event: Optional[OnEvent] = None,
) -> None:
    """
    Agentic impact analysis: single ReAct agent run covering ALL changed modules at once.
    Uses list_files / search / read_file to find files impacted by the changes.

    High- and medium-confidence hits are extracted and added to the MetaModel
    with depth=-1 (callers / impacted files).
    """
    _emit = on_event or (lambda *_, **__: None)
    visited = set(model.modules.keys())

    # Collect all changed modules that have changed symbols
    changed_modules = [
        model.modules[mid]
        for mid in model.changed_module_ids
        if mid in model.modules and any(s.is_changed for s in model.modules[mid].symbols)
    ]

    if not changed_modules:
        return

    names = ", ".join(m.name for m in changed_modules)
    _emit("searching_callers", name=names,
          path=", ".join(m.id for m in changed_modules))

    file_statuses = {path: fd.status for path, fd in diff_result.files.items()} \
        if diff_result else {}

    hits = find_impact(
        modules=changed_modules,
        repo_path=repo_path,
        llm=llm,
        model=llm_model,
        max_steps=max_agent_steps,
        max_tokens=max_agent_tokens,
        file_statuses=file_statuses,
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

        model.add(caller_mod)
        _emit("caller_found", path=hit.file, reason=hit.reason, confidence=hit.confidence)

        for dep in caller_mod.dependencies:
            if is_likely_external(dep.fqn or dep.name):
                continue
            resolved_path = resolve_dep(
                dep, caller_mod.name, repo_path, llm, llm_model, on_event=on_event,
            )
            if resolved_path:
                dep.file_path = resolved_path

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
