"""
Single-agent PR reviewer.

Two phases:
  1. Plan   — one LLM call (no tools) → structured JSON review plan
  2. Solve  — ReAct loop: 9 tools, SGR via reflect(), done() as exit

run_review(diff_text, repo_path, llm, model, ...) → (list[ReviewFinding], _Context)
"""
from __future__ import annotations
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from .streaming import stream_llm
from .prompts import load as _load_prompt
from .diff_parser import DiffResult, parse_diff
from .outline import get_outline
from .tools import list_files, read_file, search_text

log = logging.getLogger(__name__)

OnEvent = Optional[Callable[..., None]]

_STRATEGIST_SYSTEM = _load_prompt("strategist_system.txt")
_ORCHESTRATOR_SYSTEM = _load_prompt("orchestrator_system.txt")

_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "vendor",
    "dist", "build", ".git", "__pycache__", ".mypy_cache",
}

_SOLVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "List files matching a glob pattern. Returns relative paths.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read up to 100 lines of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-indexed inclusive."},
                    "end_line":   {"type": "integer", "description": "1-indexed inclusive."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_outline",
            "description": (
                "Get the structural outline of a file — classes, methods, line ranges. "
                "Use this before read_file to orient yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search for a string or regex across repo files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob":  {"type": "string", "description": "File filter, e.g. '**/*.java'."},
                    "regex": {"type": "boolean"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diff",
            "description": "Get the full diff or the diff section for a specific file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional: filter to one file."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_to_comment",
            "description": "Reply to an existing PR review comment thread.",
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["comment_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_comment",
            "description": "Mark an existing PR comment thread as resolved (issue addressed in this diff).",
            "parameters": {
                "type": "object",
                "properties": {"comment_id": {"type": "integer"}},
                "required": ["comment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reflect",
            "description": (
                "Structured self-reflection. Call every 3-5 steps to track progress, "
                "avoid going in circles, and plan the next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "learned": {
                        "type": "string",
                        "description": "Key facts established so far.",
                    },
                    "questions_remaining": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Open questions still to answer.",
                    },
                    "resolved_questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "resolution": {"type": "string", "enum": ["answered", "dropped"]},
                                "summary": {"type": "string", "description": "The answer, or reason for dropping."},
                            },
                            "required": ["question", "resolution", "summary"],
                        },
                        "description": "Questions from the previous reflect() that are now resolved. Move each question here as 'answered' (with the answer) or 'dropped' (with reason). Do not leave questions open indefinitely.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Confidence in current findings.",
                    },
                    "next_action": {
                        "type": "string",
                        "description": "What to do next and why.",
                    },
                },
                "required": ["learned", "questions_remaining", "confidence", "next_action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Submit all review findings and stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file":        {"type": "string"},
                                "line":        {"type": "integer"},
                                "severity":    {
                                    "type": "string",
                                    "enum": ["BLOCKER", "MAJOR", "MINOR", "COMMENT"],
                                },
                                "title":       {"type": "string"},
                                "explanation": {"type": "string"},
                                "evidence":    {"type": "string"},
                                "suggestion":  {"type": "string"},
                            },
                            "required": ["file", "line", "severity", "title", "explanation", "evidence"],
                        },
                    }
                },
                "required": ["findings"],
            },
        },
    },
]


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
    Single-agent review pipeline.

    Returns (findings, review_context) where review_context contains
    any queued comment replies/resolves for the caller to apply.
    """
    _emit = on_event or (lambda *_, **__: None)
    diff_result = parse_diff(diff_text)

    ctx = _Ctx(
        diff_text=diff_text,
        diff_result=diff_result,
        repo_path=repo_path,
        existing_comments=existing_comments or [],
    )

    _emit("orchestrator_plan_start")
    plan = _plan_phase(diff_text, llm, model)
    _emit("orchestrator_plan_done", plan=plan)

    findings = _solve_phase(plan, ctx, llm, model, max_steps, max_tokens, on_event)
    _emit("orchestrator_done", findings=len(findings),
          replies=len(ctx.review_context.comment_replies),
          resolves=len(ctx.review_context.comment_resolves))
    return findings, ctx.review_context


# ── Phase 1 ───────────────────────────────────────────────────────────────────

def _plan_phase(diff_text: str, llm, model: str) -> dict:
    """Single non-streaming LLM call → structured JSON plan."""
    diff_summary = _summarize_diff_for_plan(diff_text)
    try:
        response = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _STRATEGIST_SYSTEM},
                {"role": "user",   "content": diff_summary},
            ],
            temperature=0,
            stream=False,
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            content = content.rsplit("```", 1)[0].strip()
        plan = json.loads(content)
        if not isinstance(plan, dict) or "tasks" not in plan:
            raise ValueError("unexpected plan shape")
        return plan
    except Exception as exc:
        log.warning("plan phase failed (%s) — using default plan", exc)
        return {
            "system_type": "unknown",
            "tasks": [
                {
                    "id": "review_changes",
                    "type": "business_logic",
                    "priority": "high",
                    "focus": "Review the changed code for correctness and potential issues",
                    "search_hints": [],
                }
            ],
        }


# ── Phase 2 ───────────────────────────────────────────────────────────────────

def _solve_phase(
    plan: dict,
    ctx: _Ctx,
    llm,
    model: str,
    max_steps: int,
    max_tokens: int,
    on_event: OnEvent,
) -> list[ReviewFinding]:
    _emit = on_event or (lambda *_, **__: None)

    system_content = _ORCHESTRATOR_SYSTEM.format(
        diff_summary=_make_diff_summary(ctx.diff_result),
        plan=json.dumps(plan, indent=2, ensure_ascii=False),
        existing_comments=_format_existing_comments(ctx.existing_comments),
    )

    messages: list[dict] = [{"role": "system", "content": system_content}]
    total_tokens = tok_in = tok_out = tok_cached = 0
    nudge_50 = nudge_75 = False

    for step in range(max_steps):
        if total_tokens >= max_tokens:
            _emit("orchestrator_forced_done", reason="token limit",
                  tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
            break

        if total_tokens > 0:
            ratio = total_tokens / max_tokens
            if not nudge_50 and ratio >= 0.5:
                messages.append({"role": "user", "content":
                    "Half your token budget used. Focus on high-priority tasks only."})
                nudge_50 = True
            elif not nudge_75 and ratio >= 0.75:
                messages.append({"role": "user", "content":
                    "Token budget 75% used. Call done() soon with your findings."})
                nudge_75 = True

        def _on_token(tn: str, args: str, tok: int) -> None:
            _emit("orchestrator_stream", step=step, tool_name=tn, args_preview=args[:80], tok=tok)

        try:
            response = stream_llm(llm, model, messages, _SOLVE_TOOLS,
                                  tool_choice="required", on_token=_on_token)
        except Exception as exc:
            log.warning("orchestrator step %d failed: %s", step, exc)
            break

        if response.usage:
            tok_in       = response.usage.prompt_tokens
            tok_out      = response.usage.completion_tokens
            total_tokens = response.usage.total_tokens
            tok_cached   = _extract_cached(response.usage)

        msg = response.choices[0].message
        if not msg.tool_calls:
            break

        done_tc = None
        dispatch_tcs = []
        for tc in msg.tool_calls:
            if tc.function.name == "done":
                done_tc = tc
            else:
                dispatch_tcs.append(tc)

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if tc.function.name == "reflect":
                _emit("orchestrator_reflect", step=step, **args)
            else:
                _emit("orchestrator_step", step=step, tool=tc.function.name, args=args,
                      tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)

        dispatch_results: dict[str, object] = {}
        if dispatch_tcs:
            def _run(tc):
                try:
                    a = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    a = {}
                return tc.id, _dispatch(tc.function.name, a, ctx)

            with ThreadPoolExecutor(max_workers=max(1, len(dispatch_tcs))) as executor:
                futures = {executor.submit(_run, tc): tc for tc in dispatch_tcs}
                for future in as_completed(futures):
                    tc_id, result = future.result()
                    dispatch_results[tc_id] = result

        messages.append({
            "role": "assistant",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        findings_from_done = None
        for tc in msg.tool_calls:
            if tc.function.name == "done":
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                findings_from_done = _parse_findings(args.get("findings", []))
                content = "Review submitted."
            elif tc.function.name == "reflect":
                content = "Reflection noted."
            else:
                result = dispatch_results.get(tc.id, "")
                _emit("orchestrator_result", step=step, tool=tc.function.name,
                      result_len=len(str(result)))
                content = _format_result(result)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        if findings_from_done is not None:
            return findings_from_done

    # Force done
    _emit("orchestrator_forced_done", reason="step limit",
          tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
    messages.append({"role": "user", "content":
        "Step limit reached. Call done() now with all findings you have so far."})
    try:
        response = stream_llm(llm, model, messages, [_SOLVE_TOOLS[-1]], tool_choice="required")
        msg = response.choices[0].message
        if msg.tool_calls:
            args = json.loads(msg.tool_calls[0].function.arguments or "{}")
            return _parse_findings(args.get("findings", []))
    except Exception as exc:
        log.warning("orchestrator forced done failed: %s", exc)

    return []


# ── tool dispatch ──────────────────────────────────────────────────────────────

def _dispatch(tool: str, args: dict, ctx: _Ctx) -> object:
    if tool == "find_files":
        pattern = args.get("pattern", "**/*")
        files = list_files(pattern, ctx.repo_path)
        return [f for f in files if not _skip_dir(f)][:50]

    if tool == "read_file":
        path = args.get("path", "")
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None and end is not None and (end - start) > 100:
            end = start + 99
        return read_file(path, ctx.repo_path, start, end) or "(file not found)"

    if tool == "read_outline":
        path = args.get("path", "")
        fd = ctx.diff_result.files.get(path)
        changed = set(fd.after_changed_lines) if fd else None
        return get_outline(path, ctx.repo_path, changed)

    if tool == "search":
        query = args.get("query", "")
        glob = args.get("glob", "**/*")
        regex = bool(args.get("regex", False))
        results = search_text(query, ctx.repo_path, glob=glob, regex=regex)
        filtered = [
            {"file": r.file, "line": r.line, "snippet": r.text, "context": r.context}
            for r in results
            if not _skip_dir(r.file)
        ]
        return filtered[:30]

    if tool == "get_diff":
        path = args.get("path")
        if path:
            fd = ctx.diff_result.files.get(path)
            if fd is None:
                return f"No diff section found for {path}"
            return _extract_file_diff(path, ctx.diff_text)
        text = ctx.diff_text
        if len(text) > 8000:
            text = text[:8000] + "\n... (truncated, use path= to get a specific file)"
        return text

    if tool == "reply_to_comment":
        ctx.review_context.comment_replies.append({
            "comment_id": args.get("comment_id"),
            "text": args.get("text", ""),
        })
        return {"status": "queued"}

    if tool == "resolve_comment":
        ctx.review_context.comment_resolves.append(args.get("comment_id"))
        return {"status": "queued"}

    return f"unknown tool: {tool}"


# ── helpers ────────────────────────────────────────────────────────────────────

def _skip_dir(path: str) -> bool:
    return any(p in _SKIP_DIRS for p in path.replace("\\", "/").split("/"))


def _extract_cached(usage) -> int:
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return cached
    return getattr(usage, "prompt_cache_hit_tokens", 0) or 0


def _format_result(result: object) -> str:
    text = (
        json.dumps(result, ensure_ascii=False, indent=2)
        if isinstance(result, (list, dict)) else str(result)
    )
    if len(text) > 6000:
        text = text[:6000] + "\n... (truncated)"
    return text


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


def _extract_file_diff(path: str, diff_text: str) -> str:
    out: list[str] = []
    in_file = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git") and f" b/{path}" in line:
            in_file = True
        elif line.startswith("diff --git") and in_file:
            break
        if in_file:
            out.append(line)
    result = "\n".join(out)
    if len(result) > 6000:
        result = result[:6000] + "\n... (truncated)"
    return result or f"No diff section found for {path}"


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
