"""
PR reviewer — one agent entry point.

The lead agent is the sole orchestrator: it analyzes the diff,
spawns reviewer agents via tool calls, and consolidates findings.
All pipeline logic lives in prompts, not in code.

Public API:
  run_review(diff_text, repo_path, llm, model, ...) → (list[ReviewFinding], ReviewContext)
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
    BudgetConfig,
    EventBus,
    EventType,
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
    base_ref: str = ""
    source_ref: str = ""
    vfs_cache: dict = field(default_factory=dict)  # ref → vfs_dir


def run_review(
    diff_text: str,
    repo_path: str,
    llm,
    model: str,
    existing_comments: Optional[list[dict]] = None,
    max_steps: int = 50,
    max_tokens: int = 50000,
    on_event: OnEvent = None,
    trace_writer: Optional[Callable] = None,
    base_ref: str = "",
    source_ref: str = "",
) -> tuple[list[ReviewFinding], ReviewContext]:
    _emit = on_event or (lambda *_, **__: None)
    diff_result = parse_diff(diff_text)

    ctx = _Ctx(
        diff_text=diff_text, diff_result=diff_result,
        repo_path=repo_path, existing_comments=existing_comments or [],
        base_ref=base_ref, source_ref=source_ref,
    )

    # ── Compile agents from .prompt files ─────────────────────────────────
    agent_registry = compile_prompts(_PROMPT_DIR, pattern="*.prompt")
    for entry in agent_registry.entries.values():
        caps = ", ".join(entry.capabilities) if entry.capabilities else "–"
        data_fields = ", ".join(entry.input_schema.keys()) if entry.input_schema else "–"
        _emit("orchestrator_agent_compiled",
              name=entry.name, mode=entry.mode.value,
              capabilities=caps, data=data_fields,
              budget_tokens=entry.budget.max_tokens,
              budget_steps=entry.budget.max_steps)

    # ── Event bus ─────────────────────────────────────────────────────────
    event_bus = EventBus()
    event_bus.set_passthrough(_adapt_events(_emit))

    # Subscribe trace writer directly to raw events (not through adapter)
    if trace_writer:
        def _make_trace_handler(et_val):
            def handler(**kw):
                trace_writer(et_val, **kw)
            return handler
        for et in EventType:
            event_bus.subscribe(et, _make_trace_handler(et.value))

    # ── Register domain tools ─────────────────────────────────────────────
    tool_registry = ToolRegistry()
    register_diffgraph_tools(tool_registry, ctx)

    # ── Build lead config ───────────────────────────────────────────
    config = agent_registry.get_config("lead")
    if not config:
        log.error("lead agent not found in prompt registry")
        return [], ctx.review_context

    # Inject data into prompt placeholders
    diff_summary = _make_diff_summary(diff_result)
    existing_comments_str = _format_existing_comments(ctx.existing_comments)
    commits_str = _get_commit_list(repo_path, base_ref, source_ref) if base_ref and source_ref else "(unavailable)"
    config.system_prompt = interpolate(
        config.system_prompt,
        diff_summary=diff_summary,
        existing_comments=existing_comments_str,
        commits=commits_str,
    )

    # Override budget from CLI params
    config.budget = BudgetConfig(
        max_tokens=max_tokens,
        max_steps=max_steps,
        pushers=[
            PusherConfig(at=0.5, type=PusherType.NUDGE,
                         message="Half budget used. Focus on high-priority tasks."),
            PusherConfig(at=0.8, type=PusherType.NUDGE,
                         message="80% budget. Consolidate findings and call done()."),
            PusherConfig(at=1.0, type=PusherType.FORCE_DONE),
        ],
    )

    # ── Register builtins and run ─────────────────────────────────────────
    sgr_tracker = SGRTracker()
    register_builtins(tool_registry, config, sgr_tracker=sgr_tracker)

    agent = Agent(
        config=config,
        tool_registry=tool_registry,
        llm=llm,
        model=model,
        event_bus=event_bus,
        agent_registry=agent_registry,
        agent_configs=agent_registry.get_all_configs(),
    )
    # Set data scope for inheritance by child agents
    agent.data_scope = {
        "diff_summary": diff_summary,
        "existing_comments": existing_comments_str,
        "commits": commits_str,
    }

    result = agent.run()

    # Store agent ref for trace collection
    _emit("orchestrator_root_agent", agent=agent)

    # ── Parse findings ────────────────────────────────────────────────────
    raw_findings = []
    if result.output is not None:
        if isinstance(result.output, list):
            raw_findings = result.output
        elif isinstance(result.output, dict):
            raw_findings = result.output.get("findings", raw_findings)
            if not raw_findings:
                raw_findings = result.output.get("tasks", raw_findings)

    findings = _parse_findings(raw_findings)

    _emit("orchestrator_done",
          findings=len(findings),
          replies=len(ctx.review_context.comment_replies),
          resolves=len(ctx.review_context.comment_resolves))

    # Clean up VFS temp dirs
    if ctx.vfs_cache:
        import shutil
        for vfs_dir in ctx.vfs_cache.values():
            shutil.rmtree(vfs_dir, ignore_errors=True)

    return findings, ctx.review_context


# ── Event adapter ─────────────────────────────────────────────────────────────

def _adapt_events(on_event: Callable) -> Callable:
    def handler(event_type: str, **kw):
        # Pass agent identity through all events
        aid = kw.get("agent_id", "")
        aname = kw.get("agent_name", "")

        if event_type == "agent_started":
            on_event("orchestrator_agent_started",
                     agent_id=aid, agent_name=aname, depth=kw.get("depth", 0))
        elif event_type == "agent_stream":
            on_event("orchestrator_stream",
                     agent_id=aid, agent_name=aname, step=kw.get("step", 0),
                     tool_name=kw.get("tool_name", ""),
                     args_preview=kw.get("args_preview", ""), tok=kw.get("tok", 0))
        elif event_type == "agent_step":
            on_event("orchestrator_step",
                     agent_id=aid, agent_name=aname, step=kw.get("step", 0),
                     tool=kw.get("tool", ""),
                     args=kw.get("args", {}),
                     tok_in=kw.get("tok_in", 0), tok_out=kw.get("tok_out", 0),
                     tok_cached=kw.get("tok_cached", 0))
        elif event_type == "agent_reflect":
            on_event("orchestrator_reflect",
                     agent_id=aid, agent_name=aname, step=kw.get("step", 0),
                     learned=kw.get("learned", ""),
                     resolved_questions=kw.get("resolved_questions", []),
                     questions_remaining=kw.get("questions_remaining", []),
                     confidence=kw.get("confidence", ""),
                     next_action=kw.get("next_action", ""))
        elif event_type == "agent_tool_result":
            on_event("orchestrator_result",
                     agent_id=aid, agent_name=aname, step=kw.get("step", 0),
                     tool=kw.get("tool", ""),
                     args=kw.get("args", {}),
                     result_len=kw.get("result_len", 0),
                     result_preview=kw.get("result_preview", ""),
                     result_count=kw.get("result_count"))
        elif event_type == "agent_done":
            on_event("orchestrator_agent_done",
                     agent_id=aid, agent_name=aname)
        elif event_type == "agent_forced_done":
            on_event("orchestrator_forced_done",
                     agent_id=aid, agent_name=aname, reason=kw.get("reason", ""),
                     tok_in=kw.get("tok_in", 0), tok_out=kw.get("tok_out", 0),
                     tok_cached=kw.get("tok_cached", 0))
        elif event_type == "agent_spawned":
            on_event("orchestrator_agent_spawned",
                     parent_id=kw.get("parent_id", ""),
                     child_id=kw.get("child_id", ""),
                     agent_name=aname or kw.get("agent_name", ""))
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


def _get_commit_list(repo_path: str, base_ref: str, source_ref: str) -> str:
    """Get oneline commit list between base and source."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--reverse", f"{base_ref}..{source_ref}"],
            cwd=repo_path, capture_output=True, text=True, check=True,
        )
        lines = result.stdout.strip()
        return lines if lines else "(single commit)"
    except Exception:
        return "(unavailable)"


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
