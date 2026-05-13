"""
Tool registry: register via decorator or YAML, generate OpenAI schemas.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
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

    def __init__(
        self,
        *,
        fix_qwen3_stringification_bug: bool = False,
        arg_repair_handlers: Optional[list] = None,
    ) -> None:
        self._tools: dict[str, ToolDef] = {}
        # Argument-repair handler chain. Walked by `dispatch()` ONLY
        # when schema validation has already failed — each handler
        # proposes a repair, the framework re-validates, the first
        # one whose proposal passes wins. Never runs preemptively on
        # already-valid args.
        #
        # `arg_repair_handlers` (caller-supplied) wins outright. If
        # not supplied and `fix_qwen3_stringification_bug=True`, the
        # default qwen3 two-handler chain is installed. Otherwise
        # the chain is empty — repair is fully opt-in.
        self.fix_qwen3_stringification_bug = fix_qwen3_stringification_bug
        if arg_repair_handlers is not None:
            self.arg_repair_handlers = list(arg_repair_handlers)
        elif fix_qwen3_stringification_bug:
            from orchestra.tools.arg_repair import default_qwen3_chain
            self.arg_repair_handlers = default_qwen3_chain()
        else:
            self.arg_repair_handlers = []

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
        new = ToolRegistry(
            fix_qwen3_stringification_bug=self.fix_qwen3_stringification_bug,
            arg_repair_handlers=list(self.arg_repair_handlers),
        )
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

    def dispatch(self, tool_name: str, args: dict, *,
                 raw_args: Optional[str] = None) -> Any:
        """Call a tool handler by name. Validates args against JSON
        Schema first; on validation failure, walks the configured
        `arg_repair_handlers` chain to attempt recovery.

        `raw_args` is the original `function.arguments` STRING the
        LLM emitted, before our JSON parse. Plumbing it through lets
        syntax-level handlers (e.g. TruncatedJsonHandler) repair the
        raw text when our parser silently fell back to `{}`. Callers
        without raw access can omit it — semantic-level handlers
        (e.g. StringifiedArgsHandler) work on `args` alone.
        """
        td = self._tools.get(tool_name)
        if td is None:
            return f"unknown tool: {tool_name}"

        # Validate args against tool's JSON Schema
        error = _validate_args(args, td.parameters)
        if error and self.arg_repair_handlers:
            # Chain-level gate: only attempt repair when the framework
            # has flagged a missing required field. Wrong-type / enum
            # / pattern violations stay the model's problem to fix —
            # we don't want any handler second-guessing actual data.
            # Putting the gate here means individual handlers don't
            # have to re-implement it and a custom handler can't
            # accidentally widen the trigger surface.
            from orchestra.tools.arg_repair import _is_missing_required
            if not _is_missing_required(error):
                return error
            for handler in self.arg_repair_handlers:
                try:
                    candidate = handler.try_repair(
                        raw_args=raw_args,
                        args=args,
                        schema=td.parameters,
                        error=error,
                    )
                except Exception:
                    # A buggy handler must NEVER take the whole call
                    # path down — log and move on. The remaining
                    # handlers + the original validation error still
                    # surface as if this one weren't there.
                    log.exception("arg_repair handler %r raised", handler.name)
                    continue
                if candidate is None:
                    continue
                if _validate_args(candidate, td.parameters) is None:
                    log.info(
                        "tool %s: arg_repair handler %r recovered args",
                        tool_name, handler.name,
                    )
                    args = candidate
                    error = None
                    break
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


def _repair_truncated_json(raw: str) -> str:
    """Drop `"key": <empty>` pairs that make qwen3 tool-call JSON
    unparseable. Idempotent — runs until a fixed point.

    Failure shape this targets (observed in plan 192 reviewer step 10
    on qwen3-6, 6 post_comment calls all malformed the same way):

        {"text": "...", "parent_id": }

    The model emitted `parent_id` as a key but with no value. Standard
    JSON dies on the bare `}` after the colon. Two regexes cover the
    three positional variants:

      `, "k": ,`   inner empty pair (comma-prefixed)
      `, "k": }`   trailing empty pair (comma-prefixed, before close)
      `{"k": ,`    leading empty pair, follow-up keys present
      `{"k": }`    leading empty pair, only key in object

    Both regexes use a `(?=[,}])` lookahead to confirm "no value" —
    we ONLY strip when the colon is immediately followed by another
    structural token. A real value (string / number / nested object)
    won't match because the lookahead would see `"`, `{`, `[`, or a
    digit instead.

    Conservative on purpose: never modifies string contents, only the
    outermost JSON structure. A `"text": "Found , here"` value is
    safe because the regex requires a *quoted key*, and the `, ` is
    inside a string literal whose surrounding quotes aren't keys.
    """
    if not isinstance(raw, str) or not raw:
        return raw
    # Three patterns applied repeatedly. Distinguishing them matters
    # because the leading-key case has to also consume its trailing
    # comma — leaving `{, "b": 2}` would just trade one parse error
    # for another.
    #
    #   inner_or_trailing: `, "k": ,` or `, "k": }`
    #     Leading comma was the separator from a prior valid pair —
    #     dropping it along with the empty key cleanly joins the
    #     remaining pairs / closes the brace.
    #
    #   leading_with_more: `{"k": ,` (object start, more pairs follow)
    #     Must consume the trailing comma + whitespace too so the
    #     repaired object doesn't start with a dangling comma.
    #
    #   leading_lonely:    `{"k": }` (object start, no more pairs)
    #     No trailing comma to consume; just drop the key.
    inner_or_trailing = re.compile(r',\s*"[^"]*"\s*:\s*(?=[,}])')
    leading_with_more = re.compile(r'([{\[])\s*"[^"]*"\s*:\s*,\s*')
    leading_lonely    = re.compile(r'([{\[])\s*"[^"]*"\s*:\s*(?=})')
    prev = None
    cur = raw
    # Bound the loop — pathological input shouldn't spin forever.
    # 8 passes is more than enough for any realistic depth of nested
    # truncations; a 9th iteration without change exits via the
    # `prev == cur` check anyway.
    for _ in range(8):
        if prev == cur:
            break
        prev = cur
        cur = inner_or_trailing.sub('', cur)
        cur = leading_with_more.sub(r'\1', cur)
        cur = leading_lonely.sub(r'\1', cur)
    return cur


def _parse_tool_arguments(raw: str, *, fix_qwen3: bool = False) -> dict:
    """Parse `tc.function.arguments` (a JSON-encoded string from the
    OpenAI tool-call protocol) into a dict.

    Returns `{}` on unrecoverable malformed input — preserves the
    existing fallback contract `agent.py` had inline. When
    `fix_qwen3=True`, multiple repair passes are tried in sequence
    on JSONDecodeError; the first one that produces a parseable
    dict wins:

      1. `_repair_truncated_json` — drops `"key": <empty>` pairs
         (qwen3 wild-type: `"parent_id": }`).
      2. `_repair_over_escaped_keys` — un-escapes top-level object
         keys when the model emitted `\\"X\\":` instead of `"X":`
         (qwen3 wild-type: reflect with `\\"learned\\": "...", \\"
         confidence\\": "..."`, → "Unterminated string" on parse).

    Repairs are tried independently against the ORIGINAL raw input;
    if one shape doesn't apply, the next runs from scratch. Chaining
    two repairs (e.g. unescape THEN drop truncated keys) is not
    currently done — a hybrid failure shape would need a dedicated
    handler. Adding one is straightforward: a new `_repair_*` helper
    + entry in the list below.

    This helper centralises what was previously a `try: json.loads /
    except: args = {}` snippet repeated all over `agent.py` — see
    the grep at orchestra/agent.py:992 for the original site. New
    parse failures should add coverage here, not at the call site.
    """
    s = raw or "{}"
    try:
        result = json.loads(s)
    except json.JSONDecodeError:
        if not fix_qwen3:
            return {}
        result = None
        for repair in (_repair_truncated_json, _repair_over_escaped_keys):
            repaired = repair(s)
            if repaired == s:
                continue
            try:
                candidate = json.loads(repaired)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        if result is None:
            return {}
    # Tool args MUST be a JSON object — arrays / scalars at the root
    # are ill-formed for our tool protocol, so coerce to `{}` to keep
    # the dispatcher's contract (it does `handler(**args)`).
    return result if isinstance(result, dict) else {}


def _repair_over_escaped_keys(raw: str) -> str:
    """Recover from a third qwen3 emission shape: top-level object
    keys emitted with backslash-escaped quotes, breaking JSON syntax.

    Wild-type (plan 218 run cbb7a22ec16d step 6, reflect call):

        {"learned": "...content...\", \"questions_remaining\": [...],
         \"confidence\": \"medium\", \"next_action\": \"...\"}

    The model wrote `\"X\"` for object keys instead of `"X"`. JSON
    parser sees `\"` as an escaped quote (literal `"` inside the
    string value), so the `learned` string keeps going past where
    it should have closed → "Unterminated string starting at 12".

    Repair: globally replace `\"` → `"` in the raw text. This
    un-escapes one layer of quote-escaping, recovering proper
    object key boundaries. Side effect: legitimate escaped quotes
    INSIDE string content (a model citing `"this"` inside `learned`)
    would also be un-escaped, but:

      - The repair only runs on JSONDecodeError fallback — input
        that parses correctly never goes through here.
      - The framework re-validates the result against the schema
        before committing; a repair that produces unparseable
        garbage or wrong-shape data is discarded.

    Sequence inside a handler chain: cheaper truncated-key repair
    tries first (different shape, distinct regex). If the input
    has BOTH over-escaped keys AND a truncated trailing key, the
    user will see this in the operator log and we'll iterate.

    Returns the (possibly unchanged) raw string. Idempotent in the
    sense that repeated calls converge — the second call has no
    `\"` left to replace."""
    if not isinstance(raw, str) or not raw:
        return raw
    if '\\"' not in raw:
        return raw  # cheap reject — nothing to un-escape
    return raw.replace('\\"', '"')


def _repair_stringified_args(args: dict, schema: dict) -> Optional[dict]:
    """Recover from the qwen3 tool-call stringification bug.

    Failure shape (see qwen-code#379, vllm#21711, observed in our
    reviewer reflect spam loop on Qwen3-Coder via vLLM): the model
    emits ONE top-level string property whose value is an escaped
    JSON object containing all the other fields it should have
    produced separately. Example for `reflect(learned, confidence,
    next_action, ...)`:

        {"learned": "...findings...\", \"confidence\": \"high\",
                    \"next_action\": \"...\""}

    `confidence`/`next_action` live INSIDE the `learned` string,
    not next to it, so JSON-Schema validation rejects with
    "'confidence' is a required property".

    This helper looks for ONE string-typed top-level property whose
    value parses as a JSON object and contains every required key
    currently missing from the top level. If found, returns a flat
    args dict with the lifted keys merged in (and the now-redundant
    stringified container dropped — its content is fully hoisted).

    Returns None if no safe repair is possible. Conservative on
    purpose: never modifies args that schema-validate; never lifts
    if the candidate string doesn't cover ALL missing-required keys.
    """
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    missing = [k for k in required if k not in args]
    if not missing:
        return None

    for key, val in args.items():
        if not isinstance(val, str):
            continue
        s = val.strip()
        # Cheap pre-check: skip strings that don't even look like
        # a JSON object (saves the parse cost on every long `learned`).
        if not (s.startswith("{") and s.endswith("}")):
            continue
        try:
            lifted = json.loads(s)
        except Exception:
            continue
        if not isinstance(lifted, dict):
            continue
        if not all(req in lifted for req in missing):
            continue
        # Build repaired payload: drop the stringified container, then
        # overlay the lifted keys. Lifted wins on overlap — the model's
        # "real" value for any duplicate key is the one it intended at
        # the inner JSON level (the top-level string is just a wrapper).
        repaired = {k: v for k, v in args.items() if k != key}
        repaired.update(lifted)
        return repaired
    return None


def _noop_handler(**kwargs: Any) -> str:
    return "(no handler configured)"
