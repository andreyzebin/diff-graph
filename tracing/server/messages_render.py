"""Human-readable rendering for an OpenAI-style messages array.

The `/api/runs/{id}/step/{agent}/{n}/messages` endpoint stores the
exact list of {role, content, tool_calls?, tool_call_id?, name?}
dicts that the LLM saw at step N. The raw JSON is the source of
truth — but for humans (and CLI consumers like `qa logs` or `qa
trace`) the array is dense and hard to skim.

This module stitches that array into a single plain-text transcript
with role banners, pretty-printed tool-call arguments, and explicit
tool-result framing. Same renderer is consumed by:

  - the QA UI's history tab (via `?as=text` on the /messages endpoint)
  - the CLI / external tooling (same endpoint, no extra round trip)
  - unit tests (this module imported directly)

Design choices:
  - Banners use box-drawing chars so they read clearly in any
    monospace pane and don't collide with `#` from markdown bodies.
  - Tool-call arguments are pretty-printed via `json.loads` when
    the string parses; left as-is otherwise (qwen3 sometimes emits
    non-JSON XML fragments and we still want to show them).
  - System / user / assistant text content is rendered verbatim
    (no truncation, no markdown massaging) — readers can scroll.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

_BAR = "━" * 60


def render_messages(messages: Iterable[dict]) -> str:
    """Render a messages array into a plain-text transcript.

    Empty or missing fields render as `(empty)` rather than blanking
    so the reader can tell the model produced an explicit nothing
    vs the renderer silently dropped a field.
    """
    parts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        parts.append(_render_one(msg))
    return "\n\n".join(parts).rstrip() + "\n"


def _render_one(msg: dict) -> str:
    role = (msg.get("role") or "?").lower()
    tool_calls = msg.get("tool_calls") or []
    content = msg.get("content")

    if role == "assistant" and tool_calls:
        return _render_tool_calls(tool_calls, content)
    if role == "tool":
        return _render_tool_result(msg)
    return _render_text(role, content)


def _banner(label: str) -> str:
    return f"{_BAR}\n  {label}\n{_BAR}"


def _render_text(role: str, content: Any) -> str:
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
    if not body:
        body = "(empty)"
    return f"{_banner(role.upper())}\n{body}"


def _render_tool_calls(tool_calls: list, content: Any) -> str:
    """Assistant turn that calls one (or more) tools. Some providers
    also include a textual `content` alongside the tool calls — when
    present, render it first so the reader sees the model's reasoning
    before the call payload."""
    blocks: list[str] = [_banner("ASSISTANT → TOOL CALL")]
    if isinstance(content, str) and content.strip():
        blocks.append(content.rstrip())
    for tc in tool_calls:
        blocks.append(_render_one_tool_call(tc))
    return "\n\n".join(blocks)


def _render_one_tool_call(tc: dict) -> str:
    fn = tc.get("function") or {}
    name = fn.get("name") or tc.get("name") or "(unnamed)"
    raw_args = fn.get("arguments")
    if raw_args is None:
        raw_args = tc.get("arguments", "")
    # Tool-call arguments are conventionally a JSON-encoded STRING
    # (OpenAI's wire format). Pretty-print when parseable; otherwise
    # keep raw so we can debug malformed payloads (e.g. qwen3 XML
    # fragments — see qwen-code#783).
    pretty = raw_args
    if isinstance(raw_args, str):
        try:
            pretty = json.dumps(json.loads(raw_args), ensure_ascii=False, indent=2)
        except Exception:
            pretty = raw_args
    elif isinstance(raw_args, (dict, list)):
        pretty = json.dumps(raw_args, ensure_ascii=False, indent=2)
    return f"▶ {name}\n{pretty}"


def render_call(envelope: dict) -> str:
    """Render a single assistant turn — the /call endpoint payload.

    Shape: `{content: str, tool_calls: list[{function:{name, arguments}}]}`.
    Tool steps show pretty-printed args; text-only steps (judges,
    mode:single agents, final done() call) show the content string.
    When both are present we render content first then the calls — same
    convention as `_render_tool_calls` inside the messages renderer."""
    if not isinstance(envelope, dict):
        return ""
    content = envelope.get("content") or ""
    tool_calls = envelope.get("tool_calls") or []
    blocks: list[str] = []
    if isinstance(content, str) and content.strip():
        blocks.append(content.rstrip())
    for tc in tool_calls:
        blocks.append(_render_one_tool_call(tc))
    if not blocks:
        return "(empty)"
    return "\n\n".join(blocks)


def render_tools(tools: list, *, tools_count: int | None = None) -> str:
    """Render the tool list the agent saw in this step's LLM request.

    OpenAI-style tool shape: `{type: "function", function: {name,
    description, parameters: {...JSON schema}}}`. We show the function
    name, the description verbatim, and a compact list of parameter
    names with their types — enough to answer "did this tool actually
    show up in the agent's options at this step, with a meaningful
    description?". The full parameter schema lives in the JSON view.

    `tools_count` is the legacy fallback for events captured BEFORE
    full schemas were stored (orchestra/trace_db.py used to drop the
    `tools` array and keep only the count). When tools is empty but
    the legacy count is non-zero, render a hint to that effect so the
    UI doesn't silently say "0 tools" for a run where the agent really
    saw N."""
    if not tools:
        if tools_count:
            return (
                f"(no tool schemas captured for this step — legacy "
                f"trace event with tools_count={tools_count} only; "
                f"rerun on current orchestra/trace_db.py to populate "
                f"the schemas)"
            )
        return "(no tools — text-only LLM call)"
    blocks: list[str] = [_banner(f"TOOLS ({len(tools)} available)")]
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            blocks.append(f"#{i + 1} (malformed entry)")
            continue
        fn = tool.get("function") or {}
        name = fn.get("name") or tool.get("name") or "(unnamed)"
        desc = (fn.get("description") or "").strip()
        params = fn.get("parameters") or {}
        param_names = []
        props = params.get("properties") if isinstance(params, dict) else None
        if isinstance(props, dict):
            required = set(params.get("required") or [])
            for pname, pschema in props.items():
                t = (pschema or {}).get("type") if isinstance(pschema, dict) else None
                marker = "*" if pname in required else " "
                param_names.append(f"{marker} {pname}: {t or '?'}")
        parts = [f"▶ {name}"]
        if desc:
            # Indent multi-line descriptions for readability.
            parts.append("\n".join("    " + ln for ln in desc.splitlines()))
        if param_names:
            parts.append("  params:\n" + "\n".join(
                "    " + p for p in param_names
            ))
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _render_tool_result(msg: dict) -> str:
    """Tool result message. `name` is the tool that produced this
    result; `tool_call_id` ties it back to the matching call earlier
    in the transcript."""
    name = msg.get("name") or "?"
    tcid = msg.get("tool_call_id")
    label = f"TOOL RESULT · {name}"
    if tcid:
        label += f"  (call={tcid})"
    body = msg.get("content")
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False, indent=2) if body is not None else "(empty)"
    if not body:
        body = "(empty)"
    return f"{_banner(label)}\n{body}"
