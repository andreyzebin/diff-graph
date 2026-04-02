from __future__ import annotations
import logging
from collections import deque
from typing import Optional

from .extractor import OnEvent, extract_module
from .impact_agent import ImpactHit, find_impact
from .lang import detect_lang, get_globs_for_lang
from .model import MetaModel
from .resolver import is_likely_external, resolve_dep
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

            model.add(caller_mod)
            _emit("caller_found", path=hit.file, referenced=module.name, reason=hit.reason, confidence=hit.confidence)

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
