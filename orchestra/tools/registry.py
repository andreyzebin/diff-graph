"""
Tool registry: register via decorator or YAML, generate OpenAI schemas.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, get_type_hints

log = logging.getLogger(__name__)

# Python type → JSON Schema type
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolDef:
    """Internal representation of a registered tool."""
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable[..., Any]
    result_limit: int = 6000
    is_builtin: bool = False  # reflect, done, spawn_agent, etc.
    hidden: bool = False      # hidden from agent tool list (data providers)
    cache: bool = False       # cache result after first call
    _cached_result: Any = field(default=None, repr=False)
    _cache_hit: bool = field(default=False, repr=False)


class ToolRegistry:
    """Central registry for agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        fn: Optional[Callable] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[dict] = None,
        result_limit: int = 6000,
        is_builtin: bool = False,
        hidden: bool = False,
        cache: bool = False,
    ) -> Callable:
        """
        Decorator or direct call.

        Usage:
            @registry.register
            def my_tool(x: str, y: int = 0) -> str: ...

            @registry.register(name="custom_name", description="...")
            def my_tool(x: str) -> str: ...

            @registry.register(hidden=True, cache=True)
            def pr_context() -> dict: ...  # data provider, not shown to agent

            registry.register(fn=my_fn, name="alias")
        """
        def _do_register(func: Callable) -> Callable:
            tool_name = name or func.__name__
            tool_desc = description or (func.__doc__ or "").strip().split("\n")[0] or tool_name
            tool_params = parameters or _infer_schema(func)
            self._tools[tool_name] = ToolDef(
                name=tool_name,
                description=tool_desc,
                parameters=tool_params,
                handler=func,
                result_limit=result_limit,
                is_builtin=is_builtin,
                hidden=hidden,
                cache=cache,
            )
            return func

        if fn is not None:
            return _do_register(fn)
        return _do_register

    def register_tool_def(self, tool_def: ToolDef) -> None:
        """Register a pre-built ToolDef directly."""
        self._tools[tool_def.name] = tool_def

    def register_capture_tool(
        self,
        *,
        name: str,
        description: str,
        parameters: dict,
    ) -> ToolDef:
        """Register a capture-style tool — no real implementation, the
        handler echoes args back as `{status: received, args: <args>}`.

        Used for test-only output channels declared by a prompt's
        frontmatter (`extra_tools:` list). The LLM gets the schema in
        the same way as any other tool; the framework records the
        call in invocations.json and surfaces it to the judge.
        Production prompts don't declare extras — only unit-test
        fixtures do.

        Idempotent: re-registering the same name overwrites the prior
        definition (lets a fixture supersede a previous run's setup
        cleanly).
        """
        def _capture(**args: Any) -> dict:
            return {"status": "received", "args": dict(args)}

        td = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=_capture,
            is_builtin=False,
        )
        self._tools[name] = td
        return td

    def register_from_yaml(self, tools_config: dict[str, dict]) -> None:
        """
        Register tools from YAML config.

        tools:
          read_file:
            handler: diffgraph.tools.read_file
            description: "Read up to 100 lines of a file."
            parameters:
              path: {type: string, required: true}
              start_line: {type: integer}
            result_limit: 6000
        """
        for tool_name, tconf in tools_config.items():
            handler_path = tconf.get("handler", "")
            handler = _import_handler(handler_path) if handler_path else _noop_handler
            params = _yaml_params_to_schema(tconf.get("parameters", {}))
            self._tools[tool_name] = ToolDef(
                name=tool_name,
                description=tconf.get("description", tool_name),
                parameters=params,
                handler=handler,
                result_limit=tconf.get("result_limit", 6000),
            )

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def get_many(self, names: list[str]) -> list[ToolDef]:
        return [self._tools[n] for n in names if n in self._tools]

    def clone(self) -> "ToolRegistry":
        """Create a shallow copy — domain tools shared, builtins can be overwritten."""
        new = ToolRegistry()
        new._tools = dict(self._tools)
        return new

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_schema(self, names: list[str]) -> list[dict]:
        """Convert named tools to OpenAI function-calling tool dicts. Excludes hidden tools."""
        result = []
        for name in names:
            td = self._tools.get(name)
            if td is None or td.hidden:
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.parameters,
                },
            })
        return result

    def dispatch(self, tool_name: str, args: dict) -> Any:
        """Call a tool handler by name. Validates args against JSON Schema first."""
        td = self._tools.get(tool_name)
        if td is None:
            return f"unknown tool: {tool_name}"

        # Validate args against tool's JSON Schema
        error = _validate_args(args, td.parameters)
        if error:
            return error

        if td.cache and td._cache_hit:
            return td._cached_result
        try:
            result = td.handler(**args)
        except TypeError as e:
            log.debug("tool dispatch type error for %s: %s", tool_name, e)
            result = td.handler(**{k: v for k, v in args.items()
                                   if k in inspect.signature(td.handler).parameters})
        if td.cache:
            td._cached_result = result
            td._cache_hit = True
        return result

    def call_data_provider(self, tool_name: str) -> Any:
        """Call a cached data-provider tool (no args). For from: resolution."""
        return self.dispatch(tool_name, {})

    def format_result(self, tool_name: str, result: Any) -> str:
        """Format and truncate a tool result for message history."""
        td = self._tools.get(tool_name)
        limit = td.result_limit if td else 6000

        if isinstance(result, (list, dict)):
            text = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            text = str(result)

        if len(text) > limit:
            text = text[:limit] + "\n... (truncated)"
        return text


# ── Internals ─────────────────────────────────────────────────────────────────

def _infer_schema(fn: Callable) -> dict:
    """Infer JSON Schema from function type annotations."""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        hint = hints.get(param_name)
        prop: dict[str, Any] = {}

        # Resolve Optional[X]
        origin = getattr(hint, "__origin__", None)
        if origin is type(None):
            continue
        # Handle Optional (Union[X, None])
        args = getattr(hint, "__args__", None)
        is_optional = False
        if args and type(None) in args:
            is_optional = True
            non_none = [a for a in args if a is not type(None)]
            hint = non_none[0] if non_none else str

        json_type = _TYPE_MAP.get(hint, "string")
        prop["type"] = json_type

        # Description from parameter annotation string (if any)
        if param.default is not inspect.Parameter.empty:
            is_optional = True

        properties[param_name] = prop
        if not is_optional:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _yaml_params_to_schema(params: dict) -> dict:
    """Convert YAML-style params to JSON Schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, pconf in params.items():
        if isinstance(pconf, dict):
            prop = {"type": pconf.get("type", "string")}
            if pconf.get("description"):
                prop["description"] = pconf["description"]
            if pconf.get("enum"):
                prop["enum"] = pconf["enum"]
            properties[pname] = prop
            if pconf.get("required"):
                required.append(pname)
        else:
            properties[pname] = {"type": "string"}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _import_handler(dotted_path: str) -> Callable:
    """Import 'module.path.func' and return the callable."""
    mod_path, _, attr = dotted_path.rpartition(".")
    if not mod_path:
        raise ValueError(f"invalid handler path: {dotted_path}")
    mod = importlib.import_module(mod_path)
    return getattr(mod, attr)


def _validate_args(args: dict, schema: dict) -> str | None:
    """Validate tool args against JSON Schema. Returns error string or None."""
    try:
        from jsonschema import validate, ValidationError
        validate(instance=args, schema=schema)
        return None
    except ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else ""
        field = f" at '{path}'" if path else ""
        return f"validation error{field}: {e.message}"
    except Exception:
        return None  # if jsonschema not installed, skip validation


def _noop_handler(**kwargs: Any) -> str:
    return "(no handler configured)"
