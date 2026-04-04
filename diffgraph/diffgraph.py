from __future__ import annotations
from typing import Optional

from .diff_parser import parse_diff
from .agents.orchestrator import OnEvent, ReviewFinding, ReviewContext, run_review


class DiffGraph:
    """
    Single-agent code review assistant.

    Usage::

        from openai import OpenAI
        from diffgraph import DiffGraph

        client = OpenAI()
        dg = DiffGraph(repo_path="./my-service", llm_client=client)
        findings, _ = dg.review(open("my.diff").read())
        for f in findings:
            print(f.severity, f.file, f.line, f.title)
    """

    def __init__(
        self,
        repo_path: str,
        llm_client,
        llm_model: str = "gpt-4o-mini",
        max_steps: int = 40,
        max_tokens: int = 40000,
    ) -> None:
        self.repo_path = repo_path
        self.llm = llm_client
        self.model = llm_model
        self.max_steps = max_steps
        self.max_tokens = max_tokens

    def review(
        self,
        diff_text: str,
        existing_comments: Optional[list[dict]] = None,
        on_event: Optional[OnEvent] = None,
    ) -> tuple[list[ReviewFinding], ReviewContext]:
        """
        Run the agentic review pipeline.

        Returns (findings, review_context).
        review_context.comment_replies / .comment_resolves list any
        comment actions the agent requested for the caller to apply.
        """
        return run_review(
            diff_text=diff_text,
            repo_path=self.repo_path,
            llm=self.llm,
            model=self.model,
            existing_comments=existing_comments,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            on_event=on_event,
        )
