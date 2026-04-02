from __future__ import annotations
from typing import Callable, Optional

from .diff_parser import DiffResult, parse_diff
from .explorer import explore, explore_callers
from .extractor import OnEvent
from .model import MetaModel, Symbol
from .renderer import render
from .tools import read_file


class DiffGraph:
    """
    Lightweight metamodel builder for code-review agents.

    Usage::

        from openai import OpenAI
        from diffgraph import DiffGraph

        client = OpenAI()
        dg = DiffGraph(repo_path="./my-service", llm_client=client)
        context = dg.build_and_render(open("my.diff").read(), depth=2)
        # inject `context` into your review agent's prompt
    """

    def __init__(
        self,
        repo_path: str,
        llm_client,
        llm_model: str = "gpt-4o-mini",
        max_tokens_in_prompt: int = 8000,
        max_callers: int = 5,
        exclude_tests: bool = True,
        max_agent_steps: int = 12,
        max_agent_tokens: int = 20000,
    ) -> None:
        self.repo_path = repo_path
        self.llm = llm_client
        self.model = llm_model
        self.max_tokens = max_tokens_in_prompt
        self.max_callers = max_callers
        self.exclude_tests = exclude_tests
        self.max_agent_steps = max_agent_steps
        self.max_agent_tokens = max_agent_tokens

    # ── public API ────────────────────────────────────────────────────────

    def build(
        self,
        diff_text: str,
        depth: int = 2,
        on_event: Optional[OnEvent] = None,
    ) -> tuple[MetaModel, DiffResult]:
        """
        Full pipeline:
          1. parse_diff  → DiffResult
          2. explore     → MetaModel (after-versions, BFS up to `depth`)
          3. mark_changed_symbols → annotate changed symbols with before/after code

        on_event(event, **kwargs) is called throughout for progress reporting.
        Returns (MetaModel, DiffResult) so callers can pass both to render().
        """
        diff_result = parse_diff(diff_text)
        meta = explore(
            start_files=diff_result.changed_files,
            repo_path=self.repo_path,
            llm=self.llm,
            model=self.model,
            max_depth=depth,
            on_event=on_event,
        )
        mark_changed_symbols(meta, diff_result, self.repo_path)
        explore_callers(
            meta,
            self.repo_path,
            self.llm,
            self.model,
            max_callers=self.max_callers,
            exclude_tests=self.exclude_tests,
            max_agent_steps=self.max_agent_steps,
            max_agent_tokens=self.max_agent_tokens,
            on_event=on_event,
        )
        return meta, diff_result

    def render(
        self,
        model: MetaModel,
        diff_result: Optional[DiffResult] = None,
    ) -> str:
        """MetaModel → text prompt context."""
        return render(model, diff_result, max_tokens=self.max_tokens)

    def build_and_render(self, diff_text: str, depth: int = 2) -> str:
        """Shortcut: raw diff → ready-to-use prompt context string."""
        meta, diff_result = self.build(diff_text, depth=depth)
        return self.render(meta, diff_result)


# ── mark_changed_symbols (also importable standalone) ────────────────────────

def mark_changed_symbols(
    model: MetaModel,
    diff_result: DiffResult,
    repo_path: str,
) -> None:
    """
    Post-processing step after explore():

    For every changed file that is present in the MetaModel:
      - Mark symbols whose line ranges overlap with after_changed_lines.
      - Populate symbol.full_code  (read from after-version file).
      - Populate symbol.before_code (extracted from diff hunks).
    """
    for file_path, file_diff in diff_result.files.items():
        if file_path not in model.modules:
            continue

        module = model.modules[file_path]
        if file_path not in model.changed_module_ids:
            model.changed_module_ids.append(file_path)

        changed_line_set = set(file_diff.after_changed_lines)

        for sym in module.symbols:
            if any(
                line in changed_line_set
                for line in range(sym.start_line, sym.end_line + 1)
            ):
                sym.is_changed = True
                sym.full_code = read_file(
                    module.id, repo_path, sym.start_line, sym.end_line
                )
                sym.before_code = _extract_before_code(sym, file_diff)

        model.changed_symbol_names.extend(
            s.name for s in module.symbols if s.is_changed
        )


def _extract_before_code(sym: Symbol, file_diff) -> Optional[str]:
    """
    Collect before-lines from all hunks whose after-range overlaps the symbol.
    Returns None if no before-lines found (new symbol).
    """
    from .diff_parser import FileDiff  # avoid circular at module level

    collected: list[str] = []
    for hunk in file_diff.hunks:
        hunk_after_end = hunk.after_start + max(len(hunk.after_lines) - 1, 0)
        # Additions: check [hunk.after_start, hunk_after_end] ∩ [sym.start_line, sym.end_line]
        additions_overlap = hunk.after_start <= sym.end_line and hunk_after_end >= sym.start_line
        # Deletions: check if any deletion position falls inside the symbol
        deletions_overlap = any(
            sym.start_line <= p <= sym.end_line
            for p in hunk.deletion_positions
        )
        if (additions_overlap or deletions_overlap) and hunk.before_lines:
            collected.extend(hunk.before_lines)

    if not collected:
        return None
    return "\n".join(collected)
