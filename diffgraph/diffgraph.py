from __future__ import annotations
from typing import Callable, Optional

from .diff_parser import DiffResult, parse_diff
from .explorer import explore, explore_callers
from .agents.extractor import OnEvent
from .model import MetaModel
from .renderer import render
from .agents.review import apply_selections, find_review_context
from .agents.planner import plan_review
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
        review_agent_steps: int = 32,
        review_agent_tokens: int = 30000,
        review_context_budget: int = 6000,
    ) -> None:
        self.repo_path = repo_path
        self.llm = llm_client
        self.model = llm_model
        self.max_tokens = max_tokens_in_prompt
        self.max_callers = max_callers
        self.exclude_tests = exclude_tests
        self.max_agent_steps = max_agent_steps
        self.max_agent_tokens = max_agent_tokens
        self.review_agent_steps = review_agent_steps
        self.review_agent_tokens = review_agent_tokens
        self.review_context_budget = review_context_budget

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
            diff_result=diff_result,
            on_event=on_event,
        )
        return meta, diff_result

    def render(
        self,
        model: MetaModel,
        diff_result: Optional[DiffResult] = None,
        pr_title: str = "",
        pr_description: str = "",
    ) -> str:
        """MetaModel → text prompt context (mechanical BFS renderer)."""
        return render(model, diff_result, repo_path=self.repo_path, max_tokens=self.max_tokens,
                      pr_title=pr_title, pr_description=pr_description)

    def review(
        self,
        model: MetaModel,
        diff_result: Optional[DiffResult] = None,
        on_event: Optional[OnEvent] = None,
        pr_title: str = "",
        pr_description: str = "",
    ) -> str:
        """
        Run the review agent over the MetaModel to produce a curated
        code-review context. Returns the rendered string.
        """
        from .agents.review import _format_changed_block
        strategy = plan_review(
            changed_block=_format_changed_block(model),
            llm=self.llm,
            model=self.model,
            on_event=on_event,
        )
        selections = find_review_context(
            meta=model,
            repo_path=self.repo_path,
            llm=self.llm,
            model=self.model,
            max_steps=self.review_agent_steps,
            max_agent_tokens=self.review_agent_tokens,
            context_budget=self.review_context_budget,
            strategy=strategy,
            on_event=on_event,
        )
        apply_selections(model, selections)
        return render(model, diff_result, repo_path=self.repo_path, max_tokens=self.max_tokens,
                      pr_title=pr_title, pr_description=pr_description)

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
