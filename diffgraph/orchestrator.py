"""
PR reviewer — uses Orchestra's prompt-defined agents.

Preserves the original public API:
  run_review(diff_text, repo_path, llm, model, ...) → (list[ReviewFinding], ReviewContext)

Agents are defined in .prompt files, compiled into a registry.
Strategist (single-shot) → produces plan → Reviewer (react) executes it.
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
    compile_prompts,
)
from orchestra.tools.builtin import register_builtins
from orchestra.sgr import SGRTracker
from orchestra.types import PusherConfig, PusherType
from orchestra.prompts import interpolate

from .diff_parser import DiffResult, parse_diff
from .orchestra_tools import register_diffgraph_tools

log = logging.getLogger(__name__)

OnEvent = Optional[Callable[..., None]]

# Compile prompt files once at module load
_PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass
class ReviewFinding:
    file: str
    line: int
    severity: str
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

    ctx = _Ctx(
        diff_text=diff_text, diff_result=diff_result,
        repo_path=repo_path, existing_comments=existing_comments or [],
    )

    # ── Compile agent registry from .prompt files ─────────────────────────
    agent_registry = compile_prompts(_PROMPT_DIR, pattern="*.prompt")

    # ── Event bus ─────────────────────────────────────────────────────────
    event_bus = EventBus()
    event_bus.set_passthrough(_adapt_events(_emit))

    # ── Register domain tools ─────────────────────────────────────────────
    registry = ToolRegistry()
    register_diffgraph_tools(registry, ctx)

    # ── Phase 1: Strategist ───────────────────────────────────────────────
    _emit("orchestrator_plan_start")

    strategist_entry = agent_registry.get("strategist")
    if strategist_entry:
        strategist_config = strategist_entry.to_agent_config()
        # Inject diff_summary into prompt
        strategist_config = AgentConfig(
            name=strategist_config.name,
            system_prompt=interpolate(
                strategist_config.system_prompt,
                diff_summary=_summarize_diff_for_plan(diff_text),
            ),
            mode=AgentMode.SINGLE,
            budget=strategist_config.budget,
            llm_params=strategist_config.llm_params,
        )
    else:
        # Fallback if .prompt file not found
        strategist_config = AgentConfig(
            name="strategist", mode=AgentMode.SINGLE,
            system_prompt="Output a JSON review plan.",
            budget=BudgetConfig(max_tokens=5000, max_steps=1),
        )

    register_builtins(registry, strategist_config)

    strategist = Agent(
        config=strategist_config, tool_registry=registry,
        llm=llm, model=model, event_bus=event_bus,
    )
    plan_result = strategist.run()
    plan = plan_result.output if isinstance(plan_result.output, dict) else {
        "system_type": "unknown",
        "tasks": [{"id": "review", "type": "business_logic", "priority": "high",
                    "focus": "Review the changed code", "search_hints": []}],
    }

    _emit("orchestrator_plan_done", plan=plan)

    # ── Phase 2: Reviewer ─────────────────────────────────────────────────
    reviewer_entry = agent_registry.get("reviewer")
    if reviewer_entry:
        reviewer_config = reviewer_entry.to_agent_config()
        # Inject data into prompt placeholders
        reviewer_config = AgentConfig(
            name=reviewer_config.name,
            system_prompt=interpolate(
                reviewer_config.system_prompt,
                diff_summary=_make_diff_summary(diff_result),
                plan=json.dumps(plan, indent=2, ensure_ascii=False),
                existing_comments=_format_existing_comments(ctx.existing_comments),
            ),
            mode=AgentMode.REACT,
            sgr=reviewer_config.sgr,
            sgr_interval=reviewer_config.sgr_interval,
            tools=list(reviewer_config.tools),
            meta_tools=list(reviewer_config.meta_tools),
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
            llm_params=reviewer_config.llm_params,
        )
    else:
        # Fallback
        reviewer_config = AgentConfig(
            name="reviewer", mode=AgentMode.REACT, sgr=True,
            tools=["find_files", "read_file", "read_outline", "search", "get_diff",
                    "reply_to_comment", "resolve_comment"],
            budget=BudgetConfig(max_tokens=max_tokens, max_steps=max_steps),
        )

    sgr_tracker = SGRTracker()
    register_builtins(registry, reviewer_config, sgr_tracker=sgr_tracker)

    reviewer = Agent(
        config=reviewer_config, tool_registry=registry,
        llm=llm, model=model, event_bus=event_bus,
        agent_registry=agent_registry,
        agent_configs=agent_registry.get_all_configs(),
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
