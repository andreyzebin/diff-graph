"""
PR reviewer — thin wrapper around Orchestra framework.

Preserves the original public API:
  run_review(diff_text, repo_path, llm, model, ...) → (list[ReviewFinding], ReviewContext)

Internally uses Orchestra's Agent, Topology, and TopologyRunner.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestra import (
    AgentConfig,
    BudgetConfig,
    EventBus,
    OrchestraConfig,
    Topology,
    TopologyRunner,
    ToolRegistry,
)
from orchestra.config import resolve_prompt
from orchestra.tools.builtin import register_builtins
from orchestra.sgr import SGRTracker
from orchestra.types import (
    EdgeConfig,
    NodeConfig,
    NodeType,
    PusherConfig,
    PusherType,
    TopologyConfig,
)

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
    comment_replies: list[dict] = field(default_factory=list)   # [{comment_id, text}]
    comment_resolves: list[int] = field(default_factory=list)   # [comment_id, ...]


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
    """
    Review pipeline using Orchestra framework.

    Returns (findings, review_context) where review_context contains
    any queued comment replies/resolves for the caller to apply.
    """
    _emit = on_event or (lambda *_, **__: None)
    diff_result = parse_diff(diff_text)
    base_dir = Path(repo_path) if repo_path else Path(".")

    ctx = _Ctx(
        diff_text=diff_text,
        diff_result=diff_result,
        repo_path=repo_path,
        existing_comments=existing_comments or [],
    )

    # ── Build orchestra config ────────────────────────────────────────────

    strategist_prompt = resolve_prompt("diffgraph/prompts/strategist_system.txt", base_dir)
    reviewer_prompt = resolve_prompt("diffgraph/prompts/orchestrator_system.txt", base_dir)

    config = OrchestraConfig(
        agents={
            "strategist": AgentConfig(
                name="strategist",
                system_prompt=strategist_prompt,
                sgr=False,
                tools=[],
                budget=BudgetConfig(max_tokens=5000, max_steps=1),
            ),
            "reviewer": AgentConfig(
                name="reviewer",
                system_prompt=reviewer_prompt,
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
            ),
        },
        topologies={
            "default_review": TopologyConfig(
                name="default_review",
                nodes=[
                    NodeConfig(id="strategist", agent="strategist", type=NodeType.SINGLE),
                    NodeConfig(id="reviewer", agent="reviewer", type=NodeType.REACT,
                               source="strategist"),
                ],
                edges=[
                    EdgeConfig(from_node="strategist", to_node="reviewer",
                               context_handoff="findings_only"),
                ],
            ),
        },
    )

    # ── Set up event adapter ──────────────────────────────────────────────

    event_bus = EventBus()
    event_bus.set_passthrough(_adapt_events(_emit))

    # ── Register tools ────────────────────────────────────────────────────

    registry = ToolRegistry()
    register_diffgraph_tools(registry, ctx)

    # Register builtin tools (reflect, done) for the reviewer
    sgr_tracker = SGRTracker()
    register_builtins(registry, config.agents["reviewer"], sgr_tracker=sgr_tracker)

    # ── Build and run topology ────────────────────────────────────────────

    _emit("orchestrator_plan_start")

    topology = Topology(config.topologies["default_review"])

    # Prompt variables for the reviewer
    diff_summary = _make_diff_summary(diff_result)
    existing_comments_str = _format_existing_comments(ctx.existing_comments)

    runner = TopologyRunner(
        topology=topology,
        config=config,
        tool_registry=registry,
        llm=llm,
        model=model,
        event_bus=event_bus,
    )

    # Inject prompt variables: the reviewer's system prompt has
    # {diff_summary}, {plan}, {existing_comments}
    _orig_build_prompt_vars = runner._build_prompt_vars

    def _custom_prompt_vars(node):
        vars_dict = _orig_build_prompt_vars(node)
        if node.id == "reviewer":
            vars_dict["diff_summary"] = diff_summary
            vars_dict["existing_comments"] = existing_comments_str
            # Strategist output becomes {plan}
            if "strategist" in vars_dict:
                vars_dict["plan"] = vars_dict.pop("strategist")
        return vars_dict

    runner._build_prompt_vars = _custom_prompt_vars

    # Strategist needs diff summary as user input
    _orig_build_context = runner._build_context

    def _custom_build_context(node):
        context = _orig_build_context(node)
        if node.id == "strategist":
            context.append({"role": "user", "content": _summarize_diff_for_plan(diff_text)})
        return context

    runner._build_context = _custom_build_context

    result = runner.run()

    # ── Parse findings ────────────────────────────────────────────────────

    reviewer_result = result.outputs.get("reviewer")
    raw_findings = []
    if hasattr(reviewer_result, 'output') and reviewer_result.output is not None:
        if isinstance(reviewer_result.output, list):
            raw_findings = reviewer_result.output
        elif isinstance(reviewer_result.output, dict):
            raw_findings = reviewer_result.output.get("findings", [])

    findings = _parse_findings(raw_findings)

    _emit("orchestrator_done",
          findings=len(findings),
          replies=len(ctx.review_context.comment_replies),
          resolves=len(ctx.review_context.comment_resolves))

    return findings, ctx.review_context


# ── Event adapter ─────────────────────────────────────────────────────────────

def _adapt_events(on_event: Callable) -> Callable:
    """Map orchestra events → existing orchestrator_* events for cli.py."""
    _plan_emitted = {"done": False}

    def handler(event_type: str, **kw):
        if event_type == "agent_started" and kw.get("node") == "strategist":
            on_event("orchestrator_plan_start")
        elif event_type == "node_done" and kw.get("node_id") == "strategist" and not _plan_emitted["done"]:
            _plan_emitted["done"] = True
            on_event("orchestrator_plan_done", plan=kw.get("output", {}))
        elif event_type == "agent_stream":
            on_event("orchestrator_stream",
                     step=kw.get("step", 0),
                     tool_name=kw.get("tool_name", ""),
                     args_preview=kw.get("args_preview", ""),
                     tok=kw.get("tok", 0))
        elif event_type == "agent_step":
            on_event("orchestrator_step",
                     step=kw.get("step", 0),
                     tool=kw.get("tool", ""),
                     args=kw.get("args", {}),
                     tok_in=kw.get("tok_in", 0),
                     tok_out=kw.get("tok_out", 0),
                     tok_cached=kw.get("tok_cached", 0))
        elif event_type == "agent_reflect":
            on_event("orchestrator_reflect",
                     step=kw.get("step", 0),
                     learned=kw.get("learned", ""),
                     resolved_questions=kw.get("resolved_questions", []),
                     questions_remaining=kw.get("questions_remaining", []),
                     confidence=kw.get("confidence", ""),
                     next_action=kw.get("next_action", ""))
        elif event_type == "agent_tool_result":
            on_event("orchestrator_result",
                     step=kw.get("step", 0),
                     tool=kw.get("tool", ""),
                     result_len=kw.get("result_len", 0),
                     result_count=kw.get("result_count"))
        elif event_type == "agent_forced_done":
            on_event("orchestrator_forced_done",
                     reason=kw.get("reason", ""),
                     tok_in=kw.get("tok_in", 0),
                     tok_out=kw.get("tok_out", 0),
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
            file=f["file"],
            line=int(f.get("line", 1)),
            severity=f.get("severity", "MINOR"),
            title=f.get("title", ""),
            explanation=f.get("explanation", ""),
            evidence=f.get("evidence", ""),
            suggestion=f.get("suggestion", ""),
        ))
    return sorted(findings, key=lambda f: _order.get(f.severity, 2))


def _summarize_diff_for_plan(diff_text: str) -> str:
    lines = diff_text.splitlines()
    added   = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
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
