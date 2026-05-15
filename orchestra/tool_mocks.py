"""Generic Mockito-style mocking for any tool dispatched by an agent.

Every agent step is a sequence of tool calls (`agent_spawn`,
`read_file`, `search`, `pr_post_comment`, …). For isolated unit-tests
of one agent at a time we short-circuit those tool calls with canned
responses — Mockito's `when().thenReturn(a, b, c)` (sequential
answers).

Selection is STRICT ORDINAL: the i-th call to a tool consumes the
i-th entry, regardless of args. The agent's free-text focus is
LLM-shaped and varies between runs — pinning canned responses to
focus keywords makes tests brittle. Argument verification is the
judge's job, done after the run by reading the invocations.json
file (see cli.py --invocations-out).

Caveat: with strict ordinal, the reviewer's i-th investigator focus
might not match the i-th canned finding's content. The reviewer
might be confused by a finding that doesn't relate to its focus.
This is a deliberate trade-off — keeps fixtures simple, and the
reviewer's consolidation logic is what we're testing anyway, not
its faith in coherent investigator output.

Fixture file shape:

    agent_spawn:                # consumed in order
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

    read_file:                  # any tool can be mocked
      - return: "# Project rules: free item is cheapest"

Behaviour
- Tool not in the fixture at all → real dispatch (partial mocking
  is supported by design).
- Tool in the fixture but ordinal exhausted → MockExhaustedError
  (the agent made more calls to this tool than the fixture lists).
- Entry marked `sticky: true` replays forever — the ordinal counter
  stays put when a sticky entry is consumed. Use it for "disable
  this tool indefinitely; always return the same canned reply"
  cases (e.g. set_review_status while we're sorting out reviewer
  workflow noise).
- One-line shortcut: a tool whose value is a bare string is parsed
  as a single sticky entry returning that string verbatim. So
  `set_review_status: "tool temporarily off — proceed"` is
  equivalent to writing out a one-element list with
  `sticky: true` + `return: "..."`.

For `agent_spawn` specifically, the canned `return:` is wrapped into
the JSON envelope the parent agent expects from a real spawn (status
/ output / sgr_summary / steps / tokens / mocked=true). For any
other tool, the canned `return:` is passed through to
`ToolRegistry.format_result` unchanged — same shape the real tool
would have produced.

Thread safety: `ToolMocks` is shared across a parent agent and all
its mocked children. The consumed-set is protected by a Lock so
parallel agent_spawn dispatches don't race.
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
    # When True, this entry never advances the per-tool ordinal counter
    # — every subsequent call to the tool keeps returning it. Use for
    # "disable this tool indefinitely" scenarios (e.g. set_review_status
    # turned off while we work out kinks). Entries placed after a sticky
    # one are dead code (counter stuck at the sticky index).
    sticky: bool = False


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
        """Take the next ordinal entry for this tool — i-th call to
        the tool consumes the i-th entry, regardless of args.

        Why strict ordinal (and no arg matching):
        - The agent's free-text focus is LLM-shaped and varies between
          runs. Pinning the canned response to a focus substring made
          tests brittle.
        - The judge handles focus verification separately by reading
          the invocations.json file (Mockito-style verify).

        Raises MockExhaustedError when the agent makes more calls than
        the fixture lists.
        """
        entries = self.by_tool.get(tool_name)
        if not entries:
            raise MockExhaustedError(
                f"no mocks configured for tool {tool_name!r}"
            )
        with self._lock:
            consumed_set: set[int] = self._consumed.setdefault(tool_name, set())  # type: ignore
            idx = len(consumed_set)
            if idx >= len(entries):
                raise MockExhaustedError(
                    f"tool_mocks for {tool_name!r}: agent called {idx + 1} time(s) "
                    f"but fixture only lists {len(entries)} entries"
                )
            entry = entries[idx]
            # Sticky entries replay forever — counter stays put so the
            # next call returns this same entry. Non-sticky entries
            # advance the counter as usual (strict ordinal).
            if not entry.sticky:
                consumed_set.add(idx)
        return entry

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "ToolMocks":
        by_tool: dict[str, list[MockEntry]] = {}
        for tool_name, raw_entries in (data or {}).items():
            # One-line shortcut: when the value is a bare string, the
            # tool is mocked with a single sticky entry returning that
            # string. Designed for "disable this tool, just reply with
            # a fixed message" cases — the common form for
            # `set_review_status: "tool temporarily disabled"`.
            if isinstance(raw_entries, str):
                by_tool[tool_name] = [MockEntry(
                    when={}, return_data=raw_entries, sticky=True,
                )]
                continue
            if not isinstance(raw_entries, list):
                raise ValueError(
                    f"mocks for tool '{tool_name}' must be a list or string, "
                    f"got {type(raw_entries).__name__}"
                )
            entries: list[MockEntry] = []
            for i, entry in enumerate(raw_entries):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"mock entry {tool_name}[{i}] must be a mapping"
                    )
                # `when:` accepted at the YAML level for forward
                # compatibility but no longer affects selection — strict
                # ordinal only. Judge does focus verification post-run
                # via invocations.json.
                if "return" not in entry:
                    raise ValueError(
                        f"mock entry {tool_name}[{i}] missing 'return'"
                    )
                entries.append(MockEntry(
                    when={},
                    return_data=entry["return"],
                    sticky=bool(entry.get("sticky", False)),
                ))
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

    For `agent_spawn` the parent agent expects a JSON envelope with
    status / output / sgr_summary / steps / tokens. For any other
    tool the canned return is passed through verbatim.
    """
    if tool_name != "agent_spawn":
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
