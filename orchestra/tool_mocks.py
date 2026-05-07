"""Generic Mockito-style mocking for any tool dispatched by an agent.

Every agent step is a sequence of tool calls (`spawn_agent`,
`read_file`, `search`, `post_comment`, …). For isolated unit-tests
of one agent at a time we want to short-circuit some of those tool
calls with canned responses — same idea as Mockito's `when().thenReturn()`.

The most useful case is `spawn_agent`: the reviewer's heaviest cost
is the investigator chain it spawns. Replace that with a canned
finding set and the reviewer test runs in seconds.

Fixture file shape (one entry per tool, multiple matcher → return
pairs per tool):

    spawn_agent:
      - when:
          agent: investigator
          focus: ["cheapest", "free item"]    # any-of substring match
        return:
          findings:
            - severity: BLOCKER
              file: src/.../PricingService.java
              line: 95
              title: "selectFreeItem returns get(0)"
              explanation: "Per AGENTS.md the free item is the cheapest"
          confidence: high                    # → SGR summary
      - when:
          agent: investigator
          focus: ["transactional", "applyBulkDiscount"]
        return:
          findings: [...]
      - when: any                             # catch-all (optional)
        return:
          findings: []

    read_file:                                # any tool can be mocked
      - when:
          path: "AGENTS.md"
        return:
          "# Project rules ..."

Match semantics
- `when` is a mapping of arg_name → matcher_value. All listed keys
  must match (AND); keys not listed are wildcards.
- For a string matcher: case-insensitive substring of the actual.
- For a list matcher: any of the keywords as case-insensitive
  substring of the actual.
- For any other value: equality.
- `when: any` or `when: "*"` (or empty `when: {}`) → match anything.
- First matching entry wins.

Behaviour
- Tool not in the fixture at all → real dispatch (partial mocking).
- Tool in the fixture but no entry matched → MissingMockMatchError
  (fixture hole — surface immediately, don't silently slow the test).

For `spawn_agent` specifically, the canned `return` is wrapped into
the JSON envelope the parent agent expects from a real spawn (status
/ output / sgr_summary / steps / tokens / mocked=true). For any
other tool, the canned `return` is passed through to
`ToolRegistry.format_result` unchanged — same shape the real tool
would have produced.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class MockEntry:
    when: dict[str, Any]              # {arg_name: matcher}; empty = match all
    return_data: Any
    is_wildcard: bool = False         # `when: any` / `when: "*"` / `when: {}`


@dataclass
class ToolMocks:
    by_tool: dict[str, list[MockEntry]] = field(default_factory=dict)
    source_path: str = ""

    def has(self, tool_name: str) -> bool:
        return tool_name in self.by_tool

    def find(self, tool_name: str, args: dict) -> MockEntry | None:
        """First entry whose `when:` matches the given args. None means
        the tool isn't configured here at all (caller falls through to
        real dispatch). To distinguish from "configured but no match"
        — see `has()` first."""
        entries = self.by_tool.get(tool_name)
        if not entries:
            return None
        for e in entries:
            if e.is_wildcard or _entry_matches(e, args):
                return e
        return None

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "ToolMocks":
        by_tool: dict[str, list[MockEntry]] = {}
        for tool_name, raw_entries in (data or {}).items():
            if not isinstance(raw_entries, list):
                raise ValueError(
                    f"mocks for tool '{tool_name}' must be a list, got "
                    f"{type(raw_entries).__name__}"
                )
            entries: list[MockEntry] = []
            for i, entry in enumerate(raw_entries):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"mock entry {tool_name}[{i}] must be a mapping"
                    )
                when_raw = entry.get("when", {})
                ret = entry.get("return")
                is_wild = False
                when: dict[str, Any] = {}
                if when_raw in ("any", "*"):
                    is_wild = True
                elif isinstance(when_raw, dict):
                    if not when_raw:
                        is_wild = True
                    else:
                        when = dict(when_raw)
                else:
                    raise ValueError(
                        f"mock entry {tool_name}[{i}].when must be a dict, "
                        f"'any', or '*' (got {type(when_raw).__name__})"
                    )
                if "return" not in entry:
                    raise ValueError(
                        f"mock entry {tool_name}[{i}] missing 'return'"
                    )
                entries.append(MockEntry(when=when, return_data=ret, is_wildcard=is_wild))
            by_tool[tool_name] = entries
        return cls(by_tool=by_tool, source_path=source_path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ToolMocks":
        import yaml
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"mocks file not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"mocks file root must be a mapping, got {type(data).__name__}"
            )
        return cls.from_dict(data, source_path=str(p))


def _arg_matches(matcher: Any, actual: Any) -> bool:
    """Single-arg match. See module docstring for semantics."""
    if matcher in ("*", "any") or matcher is None:
        return True
    if isinstance(matcher, str):
        return matcher.lower() in str(actual or "").lower()
    if isinstance(matcher, list):
        actual_l = str(actual or "").lower()
        return any(str(kw).lower() in actual_l for kw in matcher)
    return matcher == actual


def _entry_matches(entry: MockEntry, args: dict) -> bool:
    """All listed `when:` keys must match (AND). Missing key = wildcard."""
    for key, matcher in entry.when.items():
        if not _arg_matches(matcher, args.get(key)):
            return False
    return True


def render_mock_result(tool_name: str, entry: MockEntry, args: dict) -> Any:
    """Shape the canned return value to match what the real tool would
    have produced.

    For `spawn_agent` the parent agent expects a JSON envelope with
    status / output / sgr_summary / steps / tokens. For any other
    tool the canned return is passed through verbatim.
    """
    if tool_name != "spawn_agent":
        return entry.return_data

    ret = dict(entry.return_data) if isinstance(entry.return_data, dict) else {}
    if "output" in ret:
        out = ret["output"]
        output = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    elif "findings" in ret:
        output = json.dumps({"findings": ret["findings"]}, ensure_ascii=False)
    else:
        output = json.dumps(
            {k: v for k, v in ret.items() if k != "sgr_summary"},
            ensure_ascii=False,
        )

    sgr_summary = str(ret.get("sgr_summary", "") or "")
    if not sgr_summary and "confidence" in ret:
        learned = str(ret.get("learned", "(mocked)"))[:300]
        sgr_summary = f"confidence={ret['confidence']}, learned: {learned}"

    return json.dumps({
        "status": "completed",
        "agent_id": "mock",
        "agent_name": str(args.get("agent", "")),
        "output": output,
        "sgr_summary": sgr_summary,
        "steps": 0,
        "tokens": 0,
        "mocked": True,
    }, ensure_ascii=False, indent=2, default=str)


class MissingMockMatchError(RuntimeError):
    """Raised when a tool has a mocks block configured but no entry
    matched the actual args. Indicates a fixture hole — surface
    immediately, don't silently fall through to real dispatch (would
    silently slow / change the test's nature)."""
