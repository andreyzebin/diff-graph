from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .extractor import OnEvent
from .model import Module
from .prompts import load as _load_prompt
from .tools import list_files, read_file, search_text

log = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "vendor",
    "dist", "build", ".git", "__pycache__", ".mypy_cache",
}
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".pyc", ".class",
    ".jar", ".so", ".dll", ".lock",
}
_SOURCE_GLOBS = [
    "**/*.py", "**/*.java", "**/*.ts", "**/*.tsx",
    "**/*.go", "**/*.kt", "**/*.rb", "**/*.cs",
]
_ROOT_GLOBS = ["*.yaml", "*.yml", "*.toml", "*.md"]

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files in the repo matching a glob pattern. "
                "Returns relative paths. Use to orient yourself in the project layout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py', 'src/services/*.java'",
                    }
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search for a string or regex pattern across repo files. "
                "Returns matching lines with up to 2 lines of surrounding context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "String to find. Literal by default. "
                            "Set regex=true to use regex syntax (e.g. 'def .*Service' or 'import.*model')."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional file filter glob, e.g. '**/*.py', 'src/**/*.java'. "
                            "Defaults to all files."
                        ),
                    },
                    "regex": {
                        "type": "boolean",
                        "description": "Set true to treat query as a regular expression.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a portion of a file (capped at 100 lines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "description": "1-indexed inclusive. Omit to read from beginning.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "1-indexed inclusive. Omit to read to end.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Report all impacted files and stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "impact": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file":       {"type": "string"},
                                "reason":     {"type": "string"},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                            "required": ["file", "reason", "confidence"],
                        },
                    }
                },
                "required": ["impact"],
            },
        },
    },
]

_SYSTEM_PROMPT = _load_prompt("impact_agent_system.txt")


@dataclass
class ImpactHit:
    file: str
    reason: str
    confidence: str  # "high" | "medium" | "low"


def find_impact(
    module: Module,
    repo_path: str,
    llm,
    model: str,
    max_steps: int = 12,
    max_tokens: int = 20000,
    on_event: Optional[OnEvent] = None,
) -> list[ImpactHit]:
    """
    ReAct loop: give the LLM tools (list_files, search, read_file, done)
    and let it reason about what files are impacted by changes in `module`.

    Returns ImpactHit list sorted high → medium → low.
    """
    _emit = on_event or (lambda *_, **__: None)

    changed_block = _format_changed_symbols(module)
    if not changed_block:
        return []  # nothing changed in this module — skip

    system_content = _SYSTEM_PROMPT.format(
        file_tree=_build_file_tree(repo_path),
        module_id=module.id,
        module_summary=module.summary,
        changed_symbols=changed_block,
        unchanged_symbols=_format_unchanged_symbols(module),
        max_steps=max_steps,
    )

    messages: list[dict] = [{"role": "system", "content": system_content}]
    total_tokens = 0
    tok_in = 0
    tok_out = 0
    tok_cached = 0

    for step in range(max_steps):
        if total_tokens >= max_tokens:
            _emit("agent_forced_done", path=module.id, steps=step,
                  total_tokens=total_tokens, tok_in=tok_in, tok_out=tok_out,
                  tok_cached=tok_cached, reason="token limit")
            break

        try:
            response = llm.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS,
                tool_choice="required",
                temperature=0,
            )
        except Exception as exc:
            log.warning("impact_agent step %d failed: %s", step, exc)
            break

        if response.usage:
            tok_in = response.usage.prompt_tokens
            tok_out = response.usage.completion_tokens
            total_tokens = response.usage.total_tokens
            tok_cached = _extract_cached_tokens(response.usage)

        msg = response.choices[0].message
        if not msg.tool_calls:
            break

        tc = msg.tool_calls[0]
        tool_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            args = {}

        _emit("agent_step", step=step, tool=tool_name, args=args, path=module.id,
              total_tokens=total_tokens, tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)

        if tool_name == "done":
            hits = _parse_hits(args.get("impact", []))
            _emit("agent_done", path=module.id, hits=len(hits),
                  total_tokens=total_tokens, tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
            return hits

        result = _dispatch(tool_name, args, repo_path, module.id)
        _emit("agent_result", step=step, tool=tool_name, result_len=len(str(result)), path=module.id)

        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {"name": tool_name, "arguments": tc.function.arguments},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": _format_result(result),
        })

    # Forced finish at step/token limit
    _emit("agent_forced_done", path=module.id, steps=max_steps,
          total_tokens=total_tokens, tok_in=tok_in, tok_out=tok_out,
          tok_cached=tok_cached, reason="step limit")
    messages.append({
        "role": "user",
        "content": "Step limit reached. Call done() now with what you have found so far.",
    })
    try:
        response = llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=[_TOOLS[-1]],   # only done()
            tool_choice="required",
            temperature=0,
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            args = json.loads(msg.tool_calls[0].function.arguments)
            return _parse_hits(args.get("impact", []))
    except Exception as exc:
        log.warning("impact_agent forced done() failed: %s", exc)

    return []


# ── tool dispatch ─────────────────────────────────────────────────────────────

def _dispatch(tool: str, args: dict, repo_path: str, skip_file: str) -> object:
    if tool == "list_files":
        pattern = args.get("pattern", "**/*")
        files = list_files(pattern, repo_path)
        return [f for f in files if not _skip_dir(f)]

    if tool == "search":
        query = args.get("query", "")
        glob = args.get("glob", "**/*")
        regex = bool(args.get("regex", False))
        results = search_text(query, repo_path, glob=glob, regex=regex)
        filtered = [
            {
                "file": r.file,
                "line": r.line,
                "snippet": r.text,
                "context": r.context,
            }
            for r in results
            if not _skip_dir(r.file) and not _skip_binary(r.file) and r.file != skip_file
        ]
        return filtered[:30]  # cap to avoid flooding context

    if tool == "read_file":
        path = args.get("path", "")
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None and end is not None and (end - start) > 100:
            end = start + 99
        return read_file(path, repo_path, start, end)

    return f"unknown tool: {tool}"


# ── helpers ───────────────────────────────────────────────────────────────────

def _skip_dir(path: str) -> bool:
    return any(p in _SKIP_DIRS for p in path.replace("\\", "/").split("/"))


def _skip_binary(path: str) -> bool:
    ext = ("." + path.rsplit(".", 1)[-1]) if "." in path else ""
    return ext in _BINARY_EXTS


def _extract_cached_tokens(usage) -> int:
    """Extract cached input token count from usage — handles OpenAI and DeepSeek formats."""
    # OpenAI: usage.prompt_tokens_details.cached_tokens
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return cached
    # DeepSeek: usage has prompt_cache_hit_tokens as a top-level field
    cached = getattr(usage, "prompt_cache_hit_tokens", 0)
    return cached or 0


def _parse_hits(raw: list) -> list[ImpactHit]:
    order = {"high": 0, "medium": 1, "low": 2}
    hits = [
        ImpactHit(
            file=h["file"],
            reason=h.get("reason", ""),
            confidence=h.get("confidence", "medium"),
        )
        for h in raw
        if isinstance(h, dict) and "file" in h
    ]
    return sorted(hits, key=lambda h: order.get(h.confidence, 1))


def _format_result(result: object) -> str:
    text = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (list, dict)) else str(result)
    if len(text) > 6000:
        text = text[:6000] + "\n... (truncated)"
    return text


def _build_file_tree(repo_path: str) -> str:
    all_files: list[str] = []
    for pattern in _SOURCE_GLOBS:
        all_files.extend(list_files(pattern, repo_path))

    all_files = sorted(set(f for f in all_files if not _skip_dir(f)))

    tree: dict[str, list[str]] = {}
    for f in all_files:
        parts = f.replace("\\", "/").split("/", 1)
        if len(parts) == 1:
            tree.setdefault(".", []).append(parts[0])
        else:
            tree.setdefault(parts[0] + "/", []).append(parts[1])

    lines: list[str] = []
    for dir_name, files in sorted(tree.items()):
        if dir_name == ".":
            for fname in sorted(files):
                lines.append(f"  {fname}")
        else:
            lines.append(dir_name)
            for fname in sorted(files):
                lines.append(f"  {fname}")

    return "\n".join(lines)


def _format_changed_symbols(module: Module) -> str:
    lines: list[str] = []
    for sym in module.symbols:
        if not sym.is_changed:
            continue
        lines.append(f"[{sym.kind}] {sym.signature}")
        if sym.summary:
            lines.append(f"  Summary: {sym.summary}")
        if sym.full_code is not None:
            code_lines = sym.full_code.splitlines()
            if len(code_lines) > 30:
                code_lines = code_lines[:30] + ["    ... (truncated)"]
            lines.append("  Code:")
            for line in code_lines:
                lines.append(f"    {line}")
        lines.append("")
    return "\n".join(lines)


def _format_unchanged_symbols(module: Module) -> str:
    parts = [f"[{s.kind}] {s.signature}" for s in module.symbols if not s.is_changed]
    return "\n".join(parts) if parts else "(none)"
