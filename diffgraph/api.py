from __future__ import annotations
from typing import Callable, Optional

from .diff_parser import parse_diff
from .orchestrator import OnEvent, ReviewFinding, ReviewContext, run_review


class DiffGraph:
    """
    Multi-agent code review assistant.

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
        tool_choice: str = "",
        bot_user: str = "",
    ) -> None:
        self.repo_path = repo_path
        self.llm = llm_client
        self.model = llm_model
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.tool_choice = tool_choice
        self.bot_user = bot_user

    def review(
        self,
        diff_text: str,
        existing_comments: Optional[list[dict]] = None,
        on_event: Optional[OnEvent] = None,
        trace_writer: Optional[Callable] = None,
        base_ref: str = "",
        source_ref: str = "",
        prompt_resource: Optional[str] = None,
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
            trace_writer=trace_writer,
            base_ref=base_ref,
            source_ref=source_ref,
            prompt_resource=prompt_resource,
            tool_choice=self.tool_choice,
            bot_user=self.bot_user,
        )
