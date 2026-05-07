"""Generic Mockito-style mocking for any tool dispatched by an agent.

Every agent step is a sequence of tool calls (`spawn_agent`,
`read_file`, `search`, `post_comment`, …). For isolated unit-tests
of one agent at a time we want to short-circuit some of those tool
calls with canned responses — same idea as Mockito's
`when().thenReturn(a, b, c)` (sequential answers).

The most useful case is `spawn_agent`: the reviewer's heaviest cost
is the investigator chain it spawns. The reviewer typically spawns
N investigators in one step (one per concern); each gets its own
canned finding set, in declared order.

Fixture file shape — ORDINAL: the i-th call to a tool consumes the
i-th entry. If the agent calls more times than there are entries,
the test fails loudly (better than silently falling back to a real
dispatch that would change the test's nature).

    spawn_agent:                # first three calls, in order
      - return:
          findings:
            - severity: BLOCKER
              file: src/.../PricingService.java
              line: 95
              title: "selectFreeItem returns get(0)"
          confidence: high
      - return:
          findings:
            - severity: MAJOR
              title: "Missing @Transactional on applyBulkDiscount"
      - return:
          findings:
            - severity: MINOR
              title: "Promotion entity uses manual getters"

    read_file:                   # any tool can be mocked
      - return: "# Project rules: free item is cheapest"

Optional `when:` guard
- Each entry can carry a `when:` map asserting what args the agent
  passed at this call. If `when:` is set and the actual args don't
  match → MockArgsMismatchError (the agent didn't ask the right
  thing for this slot — meaningful test failure).
- If `when:` is omitted, any args are accepted at that ordinal
  position (the test only cares that the i-th call returned X).

Match semantics for `when:`
- Map of arg_name → matcher_value. All listed keys must match (AND);
  keys not listed are wildcards.
- For a string matcher: case-insensitive substring of the actual.
- For a list matcher: any of the keywords as case-insensitive
  substring of the actual.
- For any other value: equality.

Behaviour
- Tool not in the fixture at all → real dispatch (partial mocking
  is supported by design).
- Tool in the fixture but ordinal exhausted → MockExhaustedError
  (the agent made more calls to this tool than the fixture lists).

For `spawn_agent` specifically, the canned `return:` is wrapped into
the JSON envelope the parent agent expects from a real spawn (status
/ output / sgr_summary / steps / tokens / mocked=true). For any
other tool, the canned `return:` is passed through to
`ToolRegistry.format_result` unchanged — same shape the real tool
would have produced.

Thread safety: `ToolMocks` is shared across a parent agent and all
its mocked children. The ordinal counter is protected by a Lock so
parallel spawn_agent dispatches don't race.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class MockEntry:
    when: dict[str, Any]              # optional guard; empty = no guard
    return_data: Any


@dataclass
class ToolMocks:
    by_tool: dict[str, list[MockEntry]] = field(default_factory=dict)
    source_path: str = ""
    # Per-tool set of consumed entry indices. Each entry is consumed
    # at most once over the whole run.
    _consumed: dict[str, set] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def has(self, tool_name: str) -> bool:
        return tool_name in self.by_tool

    def consume(self, tool_name: str, args: dict) -> MockEntry:
        """Take a canned response, marking the chosen entry consumed
        so it can't be reused. Selection rule (content-aware ordinal):

        1. Find the first UNCONSUMED entry whose `when:` matches the
           actual args. Each entry is consumed at most once, but the
           order in which the agent makes calls doesn't have to match
           the order entries are listed.

        2. If no `when:`-bearing entry matches, take the first
           UNCONSUMED entry that has no `when:` guard (catch-all,
           ordinal fallback).

        3. Otherwise: MockExhaustedError (agent made a call this
           fixture wasn't prepared for, or used up all slots).

        Why content-aware: real reviewers don't always spawn concerns
        in the order listed in their reflect. Strict ordinal forced
        the test author to predict the spawn order; content matching
        decouples that — each `when:` block says "when the agent
        spawns with focus matching X, return this finding" without
        caring whether it was the 1st or 3rd spawn of the run.

        Raises MockExhaustedError when no entry is available.
        """
        entries = self.by_tool.get(tool_name)
        if not entries:
            raise MockExhaustedError(
                f"no mocks configured for tool {tool_name!r}"
            )
        with self._lock:
            consumed_set: set[int] = self._consumed.setdefault(tool_name, set())  # type: ignore
            # Pass 1: prefer unconsumed entries with a matching when: guard.
            for i, e in enumerate(entries):
                if i in consumed_set:
                    continue
                if e.when and _entry_matches(e, args):
                    consumed_set.add(i)
                    return e
            # Pass 2: fall back to the first unconsumed catch-all
            # (entry without `when:` guard).
            for i, e in enumerate(entries):
                if i in consumed_set:
                    continue
                if not e.when:
                    consumed_set.add(i)
                    return e
            # Nothing matched and no catch-all left.
            raise MockExhaustedError(
                f"tool_mocks for {tool_name!r}: no unconsumed entry matches args="
                f"{args!r}; consumed {len(consumed_set)}/{len(entries)}; "
                f"remaining whens: "
                f"{[entries[i].when for i in range(len(entries)) if i not in consumed_set]}"
            )

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
                when_raw = entry.get("when")
                when: dict[str, Any] = {}
                if when_raw is None:
                    pass  # no guard
                elif when_raw in ("any", "*"):
                    pass  # treat 'any' / '*' as no guard
                elif isinstance(when_raw, dict):
                    when = dict(when_raw)
                else:
                    raise ValueError(
                        f"mock entry {tool_name}[{i}].when must be a dict, "
                        f"'any', '*', or omitted (got {type(when_raw).__name__})"
                    )
                if "return" not in entry:
                    raise ValueError(
                        f"mock entry {tool_name}[{i}] missing 'return'"
                    )
                entries.append(MockEntry(when=when, return_data=entry["return"]))
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


class MockExhaustedError(RuntimeError):
    """No unconsumed mock entry matches the agent's call (content-aware
    ordinal selection failed). Either the agent under test is doing
    more than expected, or the fixture lacks a matching entry — both
    are meaningful test signals.
    """


# Kept as alias so older tests still import.
MockArgsMismatchError = MockExhaustedError
