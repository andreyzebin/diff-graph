from __future__ import annotations
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from .extractor import OnEvent
from ..model import MetaModel
from .prompts import load as _load_prompt
from ..tools import list_files, read_file, search_text

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = _load_prompt("review_agent_system.txt")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_symbols",
            "description": (
                "Get symbols defined in a file from the dependency graph. "
                "FREE — no disk I/O. "
                "Without `lines`: returns all symbols in the file (name, kind, signature, "
                "summary, start_line, end_line, is_changed). "
                "With `lines`: returns only symbols whose line range contains at least one "
                "of those lines — use this to resolve search hits to their owning symbol."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Line numbers to resolve (from search results).",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search for text across repo files. "
                "Set context=false for a quick first-pass scan (returns file, line, and a "
                "120-char snippet only — cheap). "
                "Set context=true when you need surrounding lines to judge relevance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":   {"type": "string"},
                    "glob":    {"type": "string", "description": "File filter, e.g. '**/*.java'"},
                    "regex":   {"type": "boolean"},
                    "context": {"type": "boolean", "description": "Include ±2 surrounding lines (default true). Set false for quick scans."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file or a specific line range (1-indexed, inclusive). "
                "Use start_line/end_line from get_symbols to read a symbol's full body. "
                "Use to confirm a search hit is genuinely relevant before selecting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line":   {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": (
                "Mark a symbol as relevant for the code review context. "
                "Call this immediately when you decide a symbol is worth including — "
                "do not wait until done(). Can be called multiple times."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file":        {"type": "string", "description": "Relative file path"},
                    "symbol_name": {"type": "string"},
                    "start_line":  {"type": "integer"},
                    "end_line":    {"type": "integer"},
                    "reason":      {"type": "string", "description": "Why relevant for reviewing this change"},
                    "detail":      {"type": "string", "enum": ["full", "summary"],
                                    "description": "full=show complete code, summary=signature+summary only"},
                },
                "required": ["file", "symbol_name", "start_line", "end_line", "reason", "detail"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal that exploration is complete. All symbols marked with select() will be used.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class ReviewSelection:
    file: str
    symbol_name: str
    start_line: int
    end_line: int
    reason: str
    detail: str  # "full" | "summary"


def find_review_context(
    meta: MetaModel,
    repo_path: str,
    llm,
    model: str,
    max_steps: int = 32,
    max_agent_tokens: int = 30000,
    context_budget: int = 6000,
    strategy: str = "",
    on_event: Optional[OnEvent] = None,
) -> list[ReviewSelection]:
    """
    ReAct agent that navigates the MetaModel + repo to select the symbols
    most relevant for code review of the current diff.

    Returns a list of ReviewSelection — the curated context the agent chose.
    """
    _emit = on_event or (lambda *_, **__: None)

    changed_block = _format_changed_block(meta)
    if not changed_block:
        return []

    system_content = _SYSTEM_PROMPT.format(
        changed_block=changed_block,
        graph_overview=_build_graph_overview(meta),
        context_budget=context_budget,
        max_steps=max_steps,
    )

    _emit("review_start", changed=len(meta.changed_module_ids))

    changed_files = ", ".join(meta.changed_module_ids)
    strategy_hint = f"\n\nReview strategy: {strategy}" if strategy else ""
    messages: list[dict] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": (
            f"Changed files: {changed_files}\n"
            "Start with get_symbols on each changed file, then explore callers and deps. "
            "Call select() whenever you find a relevant symbol. "
            f"Call done() when finished (by step {max(1, max_steps - 2)} at the latest)."
            f"{strategy_hint}"
        )},
    ]
    tok_in = tok_out = tok_cached = total_tokens = 0
    nudge_50 = nudge_75 = False
    selections: list[ReviewSelection] = []

    for step in range(max_steps):
        if total_tokens >= max_agent_tokens:
            _emit("review_forced_done", reason="token limit",
                  tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
            break

        # Adaptive budget nudges based on token consumption ratio
        if total_tokens > 0:
            ratio = total_tokens / max_agent_tokens
            if not nudge_50 and ratio >= 0.5:
                messages.append({"role": "user", "content":
                    "Half your token budget is used. Prefer summary-level selections and broader searches."})
                nudge_50 = True
            elif not nudge_75 and ratio >= 0.75:
                messages.append({"role": "user", "content":
                    "Token budget 75% used. Wrap up and call done() soon."})
                nudge_75 = True

        try:
            response = llm.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS,
                tool_choice="required",
                temperature=0,
            )
        except Exception as exc:
            log.warning("review_agent step %d failed: %s", step, exc)
            break

        if response.usage:
            tok_in = response.usage.prompt_tokens
            tok_out = response.usage.completion_tokens
            total_tokens = tok_in + tok_out
            tok_cached = _extract_cached(response.usage)

        msg = response.choices[0].message
        if not msg.tool_calls:
            break

        # Separate done/select from dispatchable calls
        done_tc = None
        select_tcs = []
        dispatch_tcs = []
        for tc in msg.tool_calls:
            name = tc.function.name
            if name == "done":
                done_tc = tc
            elif name == "select":
                select_tcs.append(tc)
            else:
                dispatch_tcs.append(tc)

        # Emit step event for each tool call
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            _emit("review_step", step=step, tool=tc.function.name, args=args,
                  tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)

        # Execute dispatchable calls in parallel
        dispatch_results: dict[str, str] = {}
        if dispatch_tcs:
            def _run(tc):
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _dispatch(tc.function.name, args, meta, repo_path)
                return tc.id, _serialize(result)

            with ThreadPoolExecutor(max_workers=max(1, len(dispatch_tcs))) as executor:
                futures = {executor.submit(_run, tc): tc for tc in dispatch_tcs}
                for future in as_completed(futures):
                    tc_id, result_str = future.result()
                    dispatch_results[tc_id] = result_str

        # Process selects (in-memory, sequential)
        select_results: dict[str, str] = {}
        for tc in select_tcs:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            sel = _parse_selection(args)
            if sel:
                selections.append(sel)
                _apply_selection(meta, sel)
                _emit("review_selected", name=sel.symbol_name, file=sel.file,
                      detail=sel.detail, reason=sel.reason)
                select_results[tc.id] = f"Selected: {sel.symbol_name} ({sel.detail})"
            else:
                select_results[tc.id] = "Invalid selection — missing required fields."

        # Append assistant message with all tool calls
        messages.append({
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # Append individual tool result messages
        for tc in msg.tool_calls:
            if tc.function.name == "done":
                content = "Done."
            elif tc.function.name == "select":
                content = select_results.get(tc.id, "")
            else:
                content = dispatch_results.get(tc.id, "")
                _emit("review_result", step=step, tool=tc.function.name, result_len=len(content))
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": content,
            })

        if done_tc is not None:
            _emit("review_done", count=len(selections),
                  tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
            return selections

    _emit("review_forced_done", reason="step limit",
          tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
    return selections


# ── dispatch ──────────────────────────────────────────────────────────────────

def apply_selections(meta: MetaModel, selections: list[ReviewSelection]) -> None:
    """
    Mark symbols chosen by the review agent as is_expanded=True on the MetaModel.
    Only full-detail selections are expanded; summary stays compressed.
    """
    for sel in selections:
        if sel.detail != "full":
            continue
        mod = meta.modules.get(sel.file)
        if not mod:
            continue
        for sym in mod.symbols:
            if sym.name == sel.symbol_name:
                sym.is_expanded = True
                break


def _dispatch(tool: str, args: dict, meta: MetaModel, repo_path: str) -> object:
    if tool == "get_symbols":
        return _get_symbols(args["file_path"], args.get("lines"), meta)
    if tool == "search":
        return search_text(
            args["query"], repo_path,
            glob=args.get("glob", "**/*"),
            regex=args.get("regex", False),
            include_context=args.get("context", True),
        )
    if tool == "read_file":
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None and end is None:
            end = start + 79          # default 80-line window when end omitted
        elif end is not None:
            end = min(end, (start or 1) + 79)  # cap at 80 lines
        return read_file(args["path"], repo_path, start, end)
    return f"unknown tool: {tool}"


def _get_symbols(file_path: str, lines: Optional[list[int]], meta: MetaModel) -> object:
    mod = meta.modules.get(file_path)
    if not mod:
        return {"error": f"'{file_path}' is not in the graph. Use search or list_files to locate it."}

    symbols = mod.symbols
    if lines:
        line_set = set(lines)
        symbols = [s for s in symbols if any(s.start_line <= ln <= s.end_line for ln in line_set)]
        if not symbols:
            return {"note": f"No symbols found at lines {lines} in {file_path}",
                    "hint": "The file has these symbols",
                    "all_symbols": [f"{s.name} (lines {s.start_line}-{s.end_line})" for s in mod.symbols]}

    return [
        {
            "name": s.name,
            "kind": s.kind,
            "signature": s.signature,
            "summary": s.summary,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "is_changed": s.is_changed,
        }
        for s in symbols
    ]


# ── formatting helpers ────────────────────────────────────────────────────────

def _format_changed_block(meta: MetaModel) -> str:
    parts: list[str] = []
    for file_id in meta.changed_module_ids:
        mod = meta.modules.get(file_id)
        if not mod:
            continue
        changed = [s for s in mod.symbols if s.is_changed]
        if not changed:
            continue
        parts.append(f"### {file_id}  ({mod.summary})")
        for sym in changed:
            parts.append(f"\n[{sym.kind}] {sym.signature}")
            if sym.summary:
                parts.append(f"  Summary: {sym.summary}")
            if sym.full_code:
                code_lines = sym.full_code.splitlines()
                if len(code_lines) > 50:
                    code_lines = code_lines[:50] + ["  // ... (truncated)"]
                parts.append("  Code:")
                for line in code_lines:
                    parts.append(f"    {line}")
        parts.append("")
    return "\n".join(parts)


def _build_graph_overview(meta: MetaModel) -> str:
    depths = meta.compute_depths()
    lines: list[str] = []
    for file_id, mod in meta.modules.items():
        depth = depths.get(file_id, 0)
        label = {0: "CHANGED", -1: "caller"}.get(depth, f"dep-{depth}")
        dep_names = [d.name for d in mod.dependencies if d.file_path][:5]
        dep_str = f" → [{', '.join(dep_names)}]" if dep_names else ""
        sym_count = len(mod.symbols)
        changed_count = sum(1 for s in mod.symbols if s.is_changed)
        changed_str = f", {changed_count} changed" if changed_count else ""
        lines.append(f"  {file_id} [{label}] {sym_count} symbols{changed_str}{dep_str}")
    return "\n".join(lines)


def _parse_selection(args: dict) -> Optional[ReviewSelection]:
    try:
        return ReviewSelection(
            file=args["file"],
            symbol_name=args["symbol_name"],
            start_line=int(args["start_line"]),
            end_line=int(args["end_line"]),
            reason=args.get("reason", ""),
            detail=args.get("detail", "full"),
        )
    except (KeyError, ValueError) as e:
        log.warning("bad selection args %s: %s", args, e)
        return None


def _apply_selection(meta: MetaModel, sel: ReviewSelection) -> None:
    if sel.detail != "full":
        return
    mod = meta.modules.get(sel.file)
    if not mod:
        return
    for sym in mod.symbols:
        if sym.name == sel.symbol_name:
            sym.is_expanded = True
            break


def _serialize(result: object) -> str:
    from ..tools import SearchResult
    if isinstance(result, list) and result and isinstance(result[0], SearchResult):
        items = [{"file": r.file, "line": r.line, "text": r.text, "context": r.context}
                 for r in result]
        return json.dumps(items)
    if isinstance(result, str):
        return result
    return json.dumps(result)


def _extract_cached(usage) -> int:
    try:
        return usage.prompt_tokens_details.cached_tokens or 0
    except AttributeError:
        pass
    try:
        return getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    except Exception:
        return 0
