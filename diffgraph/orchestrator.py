"""
PR reviewer — thin wrapper around Orchestra framework.

Preserves the original public API:
  run_review(diff_text, repo_path, llm, model, ...) → (list[ReviewFinding], ReviewContext)

Creates agents directly — no topology runner. The strategist runs as
a single-shot agent, its output is passed to a react reviewer agent.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestra import (
    Agent,
    AgentConfig,
    AgentMode,
    BudgetConfig,
    EventBus,
    ToolRegistry,
)
from orchestra.config import resolve_prompt
from orchestra.tools.builtin import register_builtins
from orchestra.sgr import SGRTracker
from orchestra.types import PusherConfig, PusherType
from orchestra.prompts import interpolate

from .diff_parser import DiffResult, parse_diff
from .orchestra_tools import register_diffgraph_tools

log = logging.getLogger(__name__)

OnEvent = Optional[Callable[..., None]]


@dataclass
class ReviewFinding:
    file: str
    line: int
    severity: str   # BLOCKER | MAJOR | MINOR | COMMENT
    title: str
    explanation: str
    evidence: str
    suggestion: str = ""

    def to_dict(self) -> dict:
        d = {
            "file": self.file, "line": self.line, "severity": self.severity,
            "title": self.title, "explanation": self.explanation, "evidence": self.evidence,
        }
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


@dataclass
class ReviewContext:
    """Collects side-effectful actions the agent requested during the solve phase."""
    comment_replies: list[dict] = field(default_factory=list)
    comment_resolves: list[int] = field(default_factory=list)


@dataclass
class _Ctx:
    diff_text: str
    diff_result: DiffResult
    repo_path: str
    existing_comments: list[dict]
    review_context: ReviewContext = field(default_factory=ReviewContext)


def run_review(
    diff_text: str,
    repo_path: str,
    llm,
    model: str,
    existing_comments: Optional[list[dict]] = None,
    max_steps: int = 40,
    max_tokens: int = 40000,
    on_event: OnEvent = None,
) -> tuple[list[ReviewFinding], ReviewContext]:
    _emit = on_event or (lambda *_, **__: None)
    diff_result = parse_diff(diff_text)
    base_dir = Path(repo_path) if repo_path else Path(".")

    ctx = _Ctx(
        diff_text=diff_text, diff_result=diff_result,
        repo_path=repo_path, existing_comments=existing_comments or [],
    )

    # ── Event bus with adapter ────────────────────────────────────────────
    event_bus = EventBus()
    event_bus.set_passthrough(_adapt_events(_emit))

    # ── Register tools ────────────────────────────────────────────────────
    registry = ToolRegistry()
    register_diffgraph_tools(registry, ctx)

    # ── Phase 1: Strategist (single-shot) ─────────────────────────────────
    _emit("orchestrator_plan_start")

    strategist_prompt = resolve_prompt("diffgraph/prompts/strategist_system.txt", base_dir)

    strategist = Agent(
        config=AgentConfig(
            name="strategist",
            system_prompt=strategist_prompt,
            mode=AgentMode.SINGLE,
            budget=BudgetConfig(max_tokens=5000, max_steps=1),
        ),
        tool_registry=registry,
        llm=llm, model=model, event_bus=event_bus,
        context_messages=[
            {"role": "user", "content": _summarize_diff_for_plan(diff_text)},
        ],
    )
    plan_result = strategist.run()
    plan = plan_result.output if isinstance(plan_result.output, dict) else {
        "system_type": "unknown",
        "tasks": [{"id": "review", "type": "business_logic", "priority": "high",
                    "focus": "Review the changed code", "search_hints": []}],
    }

    _emit("orchestrator_plan_done", plan=plan)

    # ── Phase 2: Reviewer (react) ─────────────────────────────────────────
    reviewer_prompt = resolve_prompt("diffgraph/prompts/orchestrator_system.txt", base_dir)
    diff_summary = _make_diff_summary(diff_result)
    existing_comments_str = _format_existing_comments(ctx.existing_comments)

    # Interpolate the reviewer prompt with plan data
    system_prompt = interpolate(
        reviewer_prompt,
        diff_summary=diff_summary,
        plan=json.dumps(plan, indent=2, ensure_ascii=False),
        existing_comments=existing_comments_str,
    )

    reviewer_config = AgentConfig(
        name="reviewer",
        system_prompt=system_prompt,
        mode=AgentMode.REACT,
        sgr=True,
        sgr_interval=3,
        tools=[
            "find_files", "read_file", "read_outline",
            "search", "get_diff",
            "reply_to_comment", "resolve_comment",
        ],
        budget=BudgetConfig(
            max_tokens=max_tokens,
            max_steps=max_steps,
            pushers=[
                PusherConfig(at=0.5, type=PusherType.NUDGE,
                             message="Half your token budget used. Focus on high-priority tasks only."),
                PusherConfig(at=0.75, type=PusherType.NUDGE,
                             message="Token budget 75% used. Call done() soon with your findings."),
                PusherConfig(at=1.0, type=PusherType.FORCE_DONE),
            ],
        ),
    )

    # Register builtins for the reviewer
    sgr_tracker = SGRTracker()
    register_builtins(registry, reviewer_config, sgr_tracker=sgr_tracker)

    reviewer = Agent(
        config=reviewer_config,
        tool_registry=registry,
        llm=llm, model=model, event_bus=event_bus,
    )
    result = reviewer.run()

    # ── Parse findings ────────────────────────────────────────────────────
    raw_findings = []
    if result.output is not None:
        if isinstance(result.output, list):
            raw_findings = result.output
        elif isinstance(result.output, dict):
            raw_findings = result.output.get("findings", [])

    findings = _parse_findings(raw_findings)

    _emit("orchestrator_done",
          findings=len(findings),
          replies=len(ctx.review_context.comment_replies),
          resolves=len(ctx.review_context.comment_resolves))

    return findings, ctx.review_context


# ── Event adapter ─────────────────────────────────────────────────────────────

def _adapt_events(on_event: Callable) -> Callable:
    """Map orchestra events → existing orchestrator_* events for cli.py."""
    def handler(event_type: str, **kw):
        if event_type == "agent_stream":
            on_event("orchestrator_stream",
                     step=kw.get("step", 0), tool_name=kw.get("tool_name", ""),
                     args_preview=kw.get("args_preview", ""), tok=kw.get("tok", 0))
        elif event_type == "agent_step":
            on_event("orchestrator_step",
                     step=kw.get("step", 0), tool=kw.get("tool", ""),
                     args=kw.get("args", {}),
                     tok_in=kw.get("tok_in", 0), tok_out=kw.get("tok_out", 0),
                     tok_cached=kw.get("tok_cached", 0))
        elif event_type == "agent_reflect":
            on_event("orchestrator_reflect",
                     step=kw.get("step", 0), learned=kw.get("learned", ""),
                     resolved_questions=kw.get("resolved_questions", []),
                     questions_remaining=kw.get("questions_remaining", []),
                     confidence=kw.get("confidence", ""),
                     next_action=kw.get("next_action", ""))
        elif event_type == "agent_tool_result":
            on_event("orchestrator_result",
                     step=kw.get("step", 0), tool=kw.get("tool", ""),
                     result_len=kw.get("result_len", 0),
                     result_count=kw.get("result_count"))
        elif event_type == "agent_forced_done":
            on_event("orchestrator_forced_done",
                     reason=kw.get("reason", ""),
                     tok_in=kw.get("tok_in", 0), tok_out=kw.get("tok_out", 0),
                     tok_cached=kw.get("tok_cached", 0))
    return handler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_findings(raw: list) -> list[ReviewFinding]:
    _order = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "COMMENT": 3}
    findings = []
    for f in raw:
        if not isinstance(f, dict) or "file" not in f:
            continue
        findings.append(ReviewFinding(
            file=f["file"], line=int(f.get("line", 1)),
            severity=f.get("severity", "MINOR"), title=f.get("title", ""),
            explanation=f.get("explanation", ""), evidence=f.get("evidence", ""),
            suggestion=f.get("suggestion", ""),
        ))
    return sorted(findings, key=lambda f: _order.get(f.severity, 2))


def _summarize_diff_for_plan(diff_text: str) -> str:
    lines = diff_text.splitlines()
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    files_changed = [l[6:] for l in lines if l.startswith("+++ b/")]
    header = (
        f"Files changed ({len(files_changed)}): {', '.join(files_changed[:15])}\n"
        f"Total: +{added} -{removed} lines\n\n"
    )
    return header + "\n".join(lines[:200])


def _make_diff_summary(diff_result: DiffResult) -> str:
    parts = []
    for path, fd in diff_result.files.items():
        parts.append(
            f"  [{fd.status.upper()}] {path}"
            f"  (+{len(fd.after_changed_lines)} lines changed)"
        )
    return "\n".join(parts)


def _format_existing_comments(comments: list[dict]) -> str:
    if not comments:
        return "(none)"
    lines = []
    for c in comments:
        resolved = " [RESOLVED]" if c.get("resolved") else ""
        lines.append(
            f"  #{c['id']}  {c.get('file', '')}:{c.get('line', '')} — "
            f"{str(c.get('text', ''))[:100]}{resolved}"
        )
    return "\n".join(lines)
