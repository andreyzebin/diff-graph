"""Mock substrate for spawn_agent — enables isolated testing of one
agent at a time by short-circuiting `spawn_agent` calls with canned
responses defined in a YAML fixture.

Why: full-stack runs spend most of their LLM budget inside the
investigator chain spawned by the reviewer. To unit-test the
reviewer's logic (concerns + consolidation) we don't actually need
real investigators — we need predictable subagent results so the
reviewer's behaviour is the only variable. Same idea for unit-testing
the dispatcher: when /review is the trigger, mock the reviewer.

Mock file shape:
    investigator:
      - when_focus_matches: ["free item", "cheapest"]
        return:
          findings:
            - severity: BLOCKER
              file: src/.../PricingService.java
              line: 95
              title: "selectFreeItem returns get(0) instead of cheapest"
              explanation: "..."
              evidence: "..."
          confidence: high          # → SGR summary the reviewer reads
      - when_focus_matches: "*"     # explicit fallback (optional)
        return:
          findings: []
    reviewer:
      - when_focus_matches: any     # /review doesn't pass focus
        return:
          findings: [...]
          verdict: NEEDS_WORK

Match: case-insensitive substring across the keywords; first entry
wins; "*" or "any" matches anything; missing match in a configured
agent name is an explicit error (the test author left a hole in
their mocks). Agents without an entry in the file fall through to
real spawn — partial mocking is allowed.

The mocked spawn returns the same JSON shape as the real
`_meta_spawn_agent` so the parent agent can't tell the difference.
A `mocked: true` flag is added so tracing can highlight it.
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
    keywords: list[str]               # lowercased; "*" / "any" → wildcard
    return_data: dict[str, Any]       # raw "return:" mapping from the YAML

    @property
    def is_wildcard(self) -> bool:
        return any(k in ("*", "any") for k in self.keywords)


@dataclass
class SpawnMocks:
    by_agent: dict[str, list[MockEntry]] = field(default_factory=dict)
    source_path: str = ""

    def has(self, agent_name: str) -> bool:
        return agent_name in self.by_agent

    def find(self, agent_name: str, focus: str) -> MockEntry | None:
        """Return the first matching entry for (agent_name, focus).

        None means: this agent isn't configured here at all (caller
        should fall through to real spawn). To distinguish from
        "configured but no match" — see `has()` first.
        """
        entries = self.by_agent.get(agent_name)
        if not entries:
            return None
        haystack = (focus or "").lower()
        for e in entries:
            if e.is_wildcard:
                return e
            if any(kw in haystack for kw in e.keywords):
                return e
        return None

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "SpawnMocks":
        by_agent: dict[str, list[MockEntry]] = {}
        for agent_name, raw_entries in (data or {}).items():
            if not isinstance(raw_entries, list):
                raise ValueError(
                    f"mocks for '{agent_name}' must be a list, got {type(raw_entries).__name__}"
                )
            entries: list[MockEntry] = []
            for i, entry in enumerate(raw_entries):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"mock entry {agent_name}[{i}] must be a mapping"
                    )
                kws_raw = entry.get("when_focus_matches", entry.get("when_called"))
                if kws_raw is None:
                    raise ValueError(
                        f"mock entry {agent_name}[{i}] missing when_focus_matches"
                    )
                if isinstance(kws_raw, str):
                    kws = [kws_raw]
                else:
                    kws = list(kws_raw)
                kws = [str(k).strip().lower() for k in kws if str(k).strip()]
                ret = entry.get("return")
                if not isinstance(ret, dict):
                    raise ValueError(
                        f"mock entry {agent_name}[{i}] missing or non-dict 'return'"
                    )
                entries.append(MockEntry(keywords=kws, return_data=ret))
            by_agent[agent_name] = entries
        return cls(by_agent=by_agent, source_path=source_path)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SpawnMocks":
        import yaml
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"mocks file not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"mocks file root must be a mapping, got {type(data).__name__}")
        return cls.from_dict(data, source_path=str(p))


def render_mock_response(entry: MockEntry, agent_name: str, focus: str) -> str:
    """Build a JSON string matching the contract of _meta_spawn_agent.

    The parent agent (reviewer / dispatcher) parses this exactly the
    same as a real spawn result — so the mock has to keep the field
    names stable: status / agent_id / agent_name / output / sgr_summary
    / steps / tokens. We add `mocked=true` so tracing can highlight
    that the spawn was synthetic.
    """
    ret = dict(entry.return_data)

    # The parent reads `output` as the textual result. Allow either
    # explicit `output` (string or dict-as-json) or auto-construct
    # from `findings` for ergonomics.
    if "output" in ret:
        out = ret["output"]
        output = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    elif "findings" in ret:
        output = json.dumps({"findings": ret["findings"]}, ensure_ascii=False)
    else:
        output = json.dumps({k: v for k, v in ret.items() if k != "sgr_summary"},
                            ensure_ascii=False)

    sgr_summary = str(ret.get("sgr_summary", "") or "")
    if not sgr_summary and "confidence" in ret:
        learned = str(ret.get("learned", "(mocked)"))[:300]
        sgr_summary = f"confidence={ret['confidence']}, learned: {learned}"

    return json.dumps({
        "status": "completed",
        "agent_id": "mock",
        "agent_name": agent_name,
        "output": output,
        "sgr_summary": sgr_summary,
        "steps": 0,
        "tokens": 0,
        "mocked": True,
        "mock_keywords": entry.keywords,
    }, ensure_ascii=False, indent=2, default=str)


class MissingMockMatchError(RuntimeError):
    """Raised when an agent has a mocks block configured but no entry
    matched the actual focus that the parent passed in. Indicates a
    test-fixture hole — surface immediately, don't fall through to a
    real spawn (would silently slow / change the test's nature)."""
