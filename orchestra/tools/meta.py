"""
Meta-dispatch tools — `list_tools` + `call_tool`.

When an agent runs with `dispatch_mode: meta` (declared in a prompt's
YAML frontmatter), the LLM sees a SINGLE stable tools array
containing only these two meta-tools. The actual tool surface is
discovered dynamically by the LLM calling `list_tools()` once;
subsequent invocations go through `call_tool(name, args)`.

Why this strategy exists alongside the native dispatch:

- **Maximum prompt-cache utilisation.** Native dispatch sends the
  full tool schema array on every request; if a test fixture
  swaps the allowed subset, the LLM provider's cache prefix
  changes too and every variant pays a cold-cache cost on first
  call. Meta keeps the LLM schema constant — only the JSON
  returned by `list_tools` varies. The variation lives in the
  message body / tool output, not the schema prefix.
- **Scales past O(20) tools.** When the tool catalogue grows
  (MCP-style registries, hundreds of providers), enumerating
  every tool's full schema in every request becomes wasteful.
  list_tools returns descriptions inline only when invoked.
- **Decouples agent's view from API-level schema.** The LLM
  reasons about a uniform "call this tool with these args"
  protocol regardless of what the underlying surface is.

Trade-offs (vs native):

- LLM spends one round-trip on a `list_tools` call before any real
  work; for short conversations that's a measurable overhead.
- Args validation runs in-process (this module's `call_tool`
  handler) rather than at the LLM provider's tool-call API. The
  LLM CAN send malformed args; we reject them with a structured
  error response, which costs another round-trip but doesn't crash
  the run.
- Cannot rely on the LLM's native `tool_choice=required`
  enforcement on the underlying tools — only on `call_tool` itself.

Implementation contract:

- `build_meta_tools(registry, allowed)` returns `(list_td, call_td)`
  — two ToolDef objects ready to be inserted into a ToolRegistry.
- `allowed` is the set of tool names this run is permitted to
  expose; meta-tools enforce the boundary.
- The `registry` argument is the same ToolRegistry the agent is
  using for everything else. call_tool looks up handlers there.
- Idempotent: calling build_meta_tools twice yields two pairs that
  share state via the captured `allowed` set; the registry just
  overwrites prior list_tools/call_tool entries.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .registry import ToolDef


# JSON Schema describing the args accepted by list_tools / call_tool.
# These two schemas are the ONLY thing the LLM sees in meta dispatch —
# they need to be stable + minimal so the prompt cache stays warm
# across runs with wildly different `allowed` sets.

_LIST_TOOLS_PARAMS: dict = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Optional substring filter. Empty / omitted = return "
                "every tool available to this run. The framework does "
                "a case-insensitive substring match against the tool's "
                "name and description. Use for big catalogues; in "
                "typical bench runs the list is short enough to skip."
            ),
        },
    },
}

_CALL_TOOL_PARAMS: dict = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Name of the tool to invoke (from list_tools output)."
            ),
        },
        "args": {
            "type": "object",
            "description": (
                "Arguments object matching the tool's own parameters "
                "schema (also from list_tools output). Pass {} for "
                "no-arg tools."
            ),
        },
    },
    "required": ["name", "args"],
}


def _matches_query(td: ToolDef, q: str) -> bool:
    """Substring match (case-insensitive) against name + description."""
    if not q:
        return True
    needle = q.lower()
    return (needle in td.name.lower()
            or needle in (td.description or "").lower())


def _missing_required(parameters: dict, args: dict) -> list[str]:
    """Return list of required-but-absent arg names. Empty list = OK."""
    required = parameters.get("required") or []
    return [r for r in required if r not in args]


def build_meta_tools(registry: Any, allowed: Iterable[str]) -> tuple[ToolDef, ToolDef]:
    """Construct list_tools + call_tool ToolDefs scoped to `allowed`.

    Returns a (list_td, call_td) pair. The handlers close over the
    given registry + allowed set, so each (registry, allowed) build
    produces an independent boundary even if the registry itself is
    shared across agents/runs.
    """
    allowed_set = set(allowed)

    def _list_tools(query: str = "") -> str:
        """Return a JSON-stringified list of tool descriptors visible
        to this run. Each entry: {name, description, parameters}.

        Returned as STRING (not dict) so the LLM gets the JSON
        verbatim in the tool-response content, matching how it
        consumes other JSON-returning tools. The framework's
        result_limit truncation rules apply uniformly.
        """
        items = []
        for name in sorted(allowed_set):
            td = registry.get(name)
            if td is None:
                # Allowed name without a registered handler — skip
                # silently so a forgotten extra_tools registration
                # doesn't crash the list call. Caller will hit a
                # clean error on call_tool if they try to invoke it.
                continue
            if not _matches_query(td, query):
                continue
            items.append({
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            })
        return json.dumps(items, ensure_ascii=False, indent=2)

    def _call_tool(name: str = "", args: dict | str = None) -> Any:
        """Invoke the named tool with args, enforcing the run's
        allowed set + the tool's required-args schema.

        Returns the underlying tool's return value on success, or a
        structured `{error, detail}` dict on a guard violation —
        same shape the LLM gets back from any failing tool call so
        it can self-correct.
        """
        if not name:
            return {"error": "missing_name",
                    "detail": "call_tool requires a `name` argument."}
        if name not in allowed_set:
            return {"error": "tool_not_allowed",
                    "detail": (f"tool {name!r} is not in this run's "
                               f"allowed set. Available: "
                               f"{sorted(allowed_set)}")}
        td = registry.get(name)
        if td is None:
            return {"error": "tool_not_registered",
                    "detail": (f"tool {name!r} is allowed but no "
                               f"handler is registered. Likely a "
                               f"missing extra_tools entry.")}

        # Accept args as a JSON string too — some LLMs serialise the
        # nested object instead of passing it through as native
        # JSON. Be lenient on parse; strict on shape.
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError as exc:
                return {"error": "args_not_json",
                        "detail": f"args is not valid JSON: {exc}"}
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {"error": "args_not_object",
                    "detail": f"args must be an object, got {type(args).__name__}"}

        missing = _missing_required(td.parameters, args)
        if missing:
            return {"error": "missing_required_args",
                    "detail": f"required arg(s) absent: {missing}",
                    "schema": td.parameters}

        try:
            return td.handler(**args)
        except TypeError as exc:
            # Handler signature mismatch — the LLM passed an arg name
            # the underlying tool doesn't accept. Surface it cleanly
            # so the LLM can retry with corrected args.
            return {"error": "args_signature_mismatch",
                    "detail": f"{exc}",
                    "schema": td.parameters}

    list_td = ToolDef(
        name="list_tools",
        description=(
            "List tools available to you on this run. Each entry "
            "includes a `name`, a one-line `description`, and a JSON "
            "Schema `parameters`. Call once at the start to discover; "
            "re-call only if you forget. Optional `query` substring "
            "filters the list."
        ),
        parameters=_LIST_TOOLS_PARAMS,
        handler=_list_tools,
        is_builtin=True,
    )
    call_td = ToolDef(
        name="call_tool",
        description=(
            "Invoke a tool by name with structured args. The `name` "
            "must come from list_tools; `args` must match that tool's "
            "parameters schema. Returns the tool's result on success, "
            "or `{error, detail}` on a guard violation — fix and retry."
        ),
        parameters=_CALL_TOOL_PARAMS,
        handler=_call_tool,
        is_builtin=True,
    )
    return list_td, call_td
