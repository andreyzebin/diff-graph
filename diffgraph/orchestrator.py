"""
Agent entry points.

Public API:
  run_agent(agent_name, data, llm, model, ...) → dict
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
    # Reviewer's verdict on the PR as a whole. None means the agent didn't
    # call set_review_status — keep current status untouched. Otherwise one
    # of "APPROVED" / "NEEDS_WORK" / "UNAPPROVED".
    review_status: Optional[str] = None
    review_status_reason: str = ""
    # Findings the agent already published to the PR via the post_findings
    # tool (immediate-post path). Tracked here so downstream reporting
    # (run.json findings_count, trace, judge counters) sees them even when
    # nothing came back through the top-level done() forwarding chain.
    posted_findings: list["ReviewFinding"] = field(default_factory=list)


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
    # Lazy init: set _pr_url and _init_fn to defer clone until first domain tool call
    _pr_url: str = ""
    _init_fn: Optional[Callable] = None
    _initialized: bool = True

    def ensure_repo(self) -> None:
        """Clone repo lazily on first domain tool access."""
        if self._initialized:
            return
        if self._init_fn:
            self._init_fn(self)
            self._initialized = True


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
    prompt_resource: Optional[str] = None,
    tool_choice: str = "",
    bot_user: str = "",
) -> tuple[list[ReviewFinding], ReviewContext]:
    """Run lead agent directly (no dispatcher). For --pr-url without --message."""
    diff_result = parse_diff(diff_text)

    ctx = _Ctx(
        diff_text=diff_text, diff_result=diff_result,
        repo_path=repo_path, existing_comments=existing_comments or [],
        base_ref=base_ref, source_ref=source_ref,
    )
    ctx._bot_user = bot_user

    tool_registry = ToolRegistry()
    register_diffgraph_tools(tool_registry, ctx)

    # Data resolved via from:pr_context.* — no manual injection needed
    result = run_agent(
        agent_name="reviewer",
        data={},
        llm=llm,
        model=model,
        tool_registry=tool_registry,
        on_event=on_event,
        trace_writer=trace_writer,
        prompt_resource=prompt_resource,
        tool_choice=tool_choice,
    )

    raw_findings = result.get("findings", result.get("tasks", []))
    if not isinstance(raw_findings, list):
        raw_findings = []
    findings = _parse_findings(raw_findings)

    # Clean up VFS temp dirs
    if ctx.vfs_cache:
        import shutil
        for vfs_dir in ctx.vfs_cache.values():
            shutil.rmtree(vfs_dir, ignore_errors=True)

    return findings, ctx.review_context


def run_agent(
    agent_name: str,
    data: dict,
    llm,
    model: str,
    tool_registry: Optional[ToolRegistry] = None,
    on_event: OnEvent = None,
    trace_writer: Optional[Callable] = None,
    prompt_resource: Optional[str] = None,
    tool_choice: str = "",
    stream: Optional[bool] = None,
    extra_body: Optional[dict] = None,
) -> dict:
    """
    Run any prompt-defined agent by name.

    Generic entry point — no domain-specific logic. The agent's prompt
    determines what it does. Data dict is interpolated into {placeholders}.

    Returns the agent's done() output (dict), or {} if agent produced nothing.
    """
    _emit = on_event or (lambda *_, **__: None)

    prompt_source = prompt_resource or _PROMPT_DIR
    agent_registry = compile_prompts(prompt_source, pattern="*.prompt")

    config = agent_registry.get_config(agent_name)
    if not config:
        log.error("agent '%s' not found in prompt registry", agent_name)
        return {}

    # Tool registry — use provided or empty
    registry = tool_registry or ToolRegistry()

    # Single path: resolve from:tool.field + interpolate prompt
    from orchestra.agent import resolve_agent_data
    data = resolve_agent_data(config, data, registry)

    # Override LLM-level overrides for all agents (root + children).
    # tool_choice / stream / extra_body come from the provider profile, so
    # the same Qwen3-Coder hosted on a vLLM with broken streaming parser
    # transparently switches to non-streaming for every spawned investigator
    # too — without touching the prompt files.
    overrides_present = (
        bool(tool_choice) or stream is not None or extra_body is not None
    )
    if overrides_present:
        from orchestra.types import LLMParamsConfig
        for ac in [config] + list(agent_registry.get_all_configs().values()):
            if ac.llm_params is None:
                ac.llm_params = LLMParamsConfig()
            if tool_choice:
                ac.llm_params.tool_choice = tool_choice
            if stream is not None:
                ac.llm_params.stream = stream
            if extra_body is not None:
                ac.llm_params.extra_body = extra_body

    # Event bus
    event_bus = EventBus()
    event_bus.set_passthrough(_adapt_events(_emit))
    if trace_writer:
        def _make_trace_handler(et_val):
            def handler(**kw):
                trace_writer(et_val, **kw)
            return handler
        for et in EventType:
            event_bus.subscribe(et, _make_trace_handler(et.value))

    # Create agent first, then register builtins with real handlers
    sgr_tracker = SGRTracker()
    agent = Agent(
        config=config,
        tool_registry=registry,
        llm=llm,
        model=model,
        event_bus=event_bus,
        agent_registry=agent_registry,
        agent_configs=agent_registry.get_all_configs(),
    )
    register_builtins(registry, config, sgr_tracker=sgr_tracker, agent=agent)
    agent.data_scope = data

    result = agent.run()

    # Parse output
    output = result.output
    if output is None:
        return {}
    if isinstance(output, dict):
        return output
    # Agent.done(findings=[...]) — agent.py unwraps the list out of {"findings": ...},
    # so a bare list back here means "this is the findings array".
    if isinstance(output, list):
        return {"findings": output}
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return {"text": output}
        if isinstance(parsed, list):
            return {"findings": parsed}
        if isinstance(parsed, dict):
            return parsed
        return {"text": output}
    return {"output": output}


# ── Event adapter ─────────────────────────────────────────────────────────────

_EVENT_MAP = {
    "agent_started":     "orchestrator_agent_started",
    "agent_stream":      "orchestrator_stream",
    "agent_step":        "orchestrator_step",
    "agent_reflect":     "orchestrator_reflect",
    "agent_tool_result": "orchestrator_result",
    "agent_done":        "orchestrator_agent_done",
    "agent_forced_done": "orchestrator_forced_done",
    "agent_spawned":     "orchestrator_agent_spawned",
}


def _adapt_events(on_event: Callable) -> Callable:
    """Rename orchestra events to orchestrator_* for CLI. All **kw pass through."""
    def handler(event_type: str, **kw):
        mapped = _EVENT_MAP.get(event_type)
        if mapped:
            on_event(mapped, **kw)
    return handler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_findings(raw: list) -> list[ReviewFinding]:
    _order = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "COMMENT": 3}
    findings = []
    for f in raw:
        if not isinstance(f, dict) or not f.get("file"):
            continue
        # Models occasionally emit `line: null` (or strings, or floats) and
        # skip optional fields — coerce defensively so a single quirky
        # finding doesn't crash the whole post-run path.
        try:
            line = int(f.get("line") or 1)
        except (TypeError, ValueError):
            line = 1
        findings.append(ReviewFinding(
            file=str(f["file"]), line=line,
            severity=str(f.get("severity") or "MINOR"),
            title=str(f.get("title") or ""),
            explanation=str(f.get("explanation") or ""),
            evidence=str(f.get("evidence") or ""),
            suggestion=str(f.get("suggestion") or ""),
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


_MAX_COMMENTS = 20

# Optional regex for "synthetic author" recognition. When `subject_pattern`
# is set (CLI flag --subject-pattern, see cli.py), comment text is matched
# against it; if the first capture group fires, that string is used as the
# author name instead of the Bitbucket author slug. Designed for the
# benchmark suite which simulates multiple humans through a single
# Bitbucket account by prefixing comments with `[alice]`, `[bob]`, etc.
# Off by default — production users don't write `[name]` prefixes
# deliberately, but a stray match would mislabel them.
import re as _re


def _strip_subject_prefix(text: str, pattern: _re.Pattern | None) -> tuple[str, str]:
    """Return (synthetic_author, remaining_text). Empty author when no match."""
    if not pattern or not text:
        return "", text
    m = pattern.match(text)
    if not m or not m.groups():
        return "", text
    name = (m.group(1) or "").strip()
    rest = text[m.end():].lstrip()
    return name, rest


def _format_existing_comments(comments: list[dict], bot_user: str = "",
                              subject_pattern: _re.Pattern | None = None) -> str:
    """Format comments for prompt injection. Keeps unresolved + last N resolved.

    If `subject_pattern` matches a comment's text, the captured group is
    treated as the author for [SELF]/[HUMAN] labelling — overriding the
    Bitbucket author slug. Used by the bench harness to simulate a
    multi-author thread under a single account.
    """
    if not comments:
        return "(none)"

    # Always limit: keep unresolved + most recent resolved
    unresolved = [c for c in comments if not c.get("resolved")]
    resolved = [c for c in comments if c.get("resolved")]
    remaining = max(0, _MAX_COMMENTS - len(unresolved))
    filtered = unresolved + resolved[-remaining:] if remaining else unresolved
    if len(filtered) > _MAX_COMMENTS:
        filtered = filtered[-_MAX_COMMENTS:]

    lines = []
    for c in filtered:
        resolved = " [RESOLVED]" if c.get("resolved") else ""
        author = c.get("author", "")
        slug = c.get("author_slug", "")
        text_raw = str(c.get("text", ""))
        synth_author, text = _strip_subject_prefix(text_raw, subject_pattern)
        if synth_author:
            # Synthetic author wins over Bitbucket slug for labelling.
            if bot_user and synth_author == bot_user:
                who = f"[SELF:{synth_author}]"
            else:
                who = f"[{synth_author}]"
        elif bot_user and slug and slug == bot_user:
            who = f"[SELF:{author}]"
        else:
            who = f"[{author}]" if author else ""
        lines.append(
            f"  #{c['id']} {who} {c.get('file', '')}:{c.get('line', '')} — "
            f"{text[:100]}{resolved}"
        )

    total = len(comments)
    shown = len(filtered)
    header = ""
    if shown < total:
        header = f"  ({shown} of {total} shown, {total - shown} older resolved omitted)\n"
    return header + "\n".join(lines)
