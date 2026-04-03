from __future__ import annotations
import json
import logging
from typing import Optional

from .extractor import OnEvent
from ..model import Dependency
from .prompts import load as _load_prompt
from ..tools import list_files, read_file, search_text

log = logging.getLogger(__name__)

# Known external package prefixes — skip agent call entirely for these
_EXTERNAL_PREFIXES = (
    # JVM
    "java.", "javax.", "jakarta.",
    "org.springframework.", "org.junit.", "org.mockito.", "org.assertj.",
    "lombok.", "org.slf4j.", "org.apache.", "org.hibernate.",
    "com.fasterxml.", "io.swagger.", "io.micrometer.",
    "kotlin.", "android.", "androidx.",
    "reactor.", "io.reactivex.",
    # Python stdlib / popular libs
    "pytest.", "unittest.", "typing.",
    "rich.", "typer.", "click.", "fastapi.", "pydantic.", "sqlalchemy.",
    "openai.", "anthropic.", "httpx.", "requests.", "flask.", "django.",
    # Go / TS / C# ecosystem
    "github.com/", "golang.org/", "k8s.io/",
)

_SYSTEM_PROMPT = _load_prompt("resolve_dependency_system.txt")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files matching a glob pattern. Returns relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob, e.g. '**/model/Order.java'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search for text across repo files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "glob": {"type": "string", "description": "Optional file filter glob."},
                    "regex": {"type": "boolean"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the beginning of a file to understand its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Report the resolved file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": ["string", "null"],
                        "description": "Relative path to the defining file, or null if external/not found.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]


def is_likely_external(fqn: str) -> bool:
    """Return True if the FQN looks like an external library we should skip."""
    return any(fqn.startswith(p) for p in _EXTERNAL_PREFIXES)


def resolve_dep(
    dep: Dependency,
    source_name: str,
    repo_path: str,
    llm,
    model: str,
    max_steps: int = 6,
    on_event: Optional[OnEvent] = None,
) -> str:
    """
    Resolve dep.fqn → file_path.

    Fast path: derive file path from FQN via filesystem glob (no LLM).
    Fallback: ReAct agent for non-standard layouts or ambiguous matches.

    Returns file_path, or "" if not found.
    """
    _emit = on_event or (lambda *_, **__: None)

    # Fast path: deterministic FQN → path derivation
    fast_path = _fast_resolve(dep, repo_path)
    if fast_path:
        _emit("resolved", name=dep.name, path=fast_path)
        return fast_path

    # Agent fallback for non-standard layouts
    _emit("resolving_agent", name=dep.name, fqn=dep.fqn, source=source_name)

    user_message = (
        f"Find the source file that defines: {dep.name}\n"
        f"Fully qualified name: {dep.fqn}\n"
        f"Used by module: {source_name}\n"
        f"How it is used: {dep.usage or '(not specified)'}\n"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    tok_in = tok_out = tok_cached = 0

    for step in range(max_steps):
        resp = llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=_TOOLS,
            tool_choice="required",
            temperature=0,
        )
        choice = resp.choices[0]
        msg = choice.message

        if resp.usage:
            tok_in += resp.usage.prompt_tokens
            tok_out += resp.usage.completion_tokens
            tok_cached += _extract_cached(resp.usage)

        messages.append(msg)

        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            tool = tc.function.name
            args = json.loads(tc.function.arguments)

            _emit("agent_step", step=step, tool=tool, args=args, path=dep.name,
                  tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)

            if tool == "done":
                file_path = args.get("file_path") or ""
                if file_path:
                    _emit("resolved", name=dep.name, path=file_path,
                          tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
                else:
                    _emit("not_resolved", name=dep.name,
                          tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
                return file_path

            result = _dispatch(tool, args, repo_path)
            result_str = _serialize(result)
            _emit("agent_result", step=step, tool=tool,
                  result_len=len(result_str), path=dep.name)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

    _emit("not_resolved", name=dep.name, tok_in=tok_in, tok_out=tok_out, tok_cached=tok_cached)
    return ""


def _dispatch(tool: str, args: dict, repo_path: str) -> object:
    if tool == "list_files":
        return list_files(args["pattern"], repo_path)
    if tool == "search":
        return search_text(
            args["query"], repo_path,
            glob=args.get("glob", "**/*"),
            regex=args.get("regex", False),
        )
    if tool == "read_file":
        start = args.get("start_line")
        end = args.get("end_line")
        if end is not None:
            end = min(end, (start or 1) + 59)
        return read_file(args["path"], repo_path, start, end)
    return f"unknown tool: {tool}"


def _fast_resolve(dep: Dependency, repo_path: str) -> str:
    """
    Deterministic FQN → file path without any LLM call.

    Strategy:
      1. Python: FQN "pkg.module.ClassName" → try glob "**/pkg/module.py"
                 also try "**/pkg/module/__init__.py"
      2. Java/Kotlin: FQN "com.example.foo.Bar" → try glob "**/foo/Bar.java"
      3. Fallback: plain "**/Bar.java" / "**/bar.py" name search
      4. Multiple matches → pick shortest path (closest to repo root)
      5. Return "" if nothing found (caller will use agent fallback)
    """
    fqn = dep.fqn or dep.name
    name = dep.name
    parts = fqn.split(".")

    candidates: list[str] = []

    # Step 1: Python module path — all parts except last form the file path
    if len(parts) >= 2:
        module_path = "/".join(parts[:-1])
        for pattern in (f"**/{module_path}.py", f"**/{module_path}/__init__.py"):
            hits = list_files(pattern, repo_path)
            if hits:
                candidates.extend(hits)
                break

    # Step 2: Java/Kotlin — parent package segment as directory hint
    if not candidates and len(parts) >= 2:
        parent_dir = parts[-2]
        for ext in (".java", ".kt", ".ts", ".tsx", ".cs", ".rb", ".go"):
            hits = list_files(f"**/{parent_dir}/{name}{ext}", repo_path)
            if hits:
                candidates.extend(hits)
                break

    # Step 3: plain name-based glob as fallback
    if not candidates:
        for ext in (".java", ".kt", ".py", ".go", ".ts", ".tsx", ".cs", ".rb"):
            hits = list_files(f"**/{name}{ext}", repo_path)
            if hits:
                candidates.extend(hits)
                break

    if not candidates:
        return ""
    return min(candidates, key=len)  # shortest path = closest to root


def _serialize(result: object) -> str:
    """Convert tool result to a JSON string safe for LLM message content."""
    from .tools import SearchResult
    if isinstance(result, list) and result and isinstance(result[0], SearchResult):
        items = [
            {"file": r.file, "line": r.line, "text": r.text, "context": r.context}
            for r in result
        ]
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
