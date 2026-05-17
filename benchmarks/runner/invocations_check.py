"""Deterministic invocation-pattern assertions for unit fixtures.

Complements the LLM judge: the judge grades semantic content (did
the agent SAY the right thing?), this module grades structural
behaviour (did the agent CALL the right tools?). Used by tests like
SKILL-001 where the whole point is the call pattern (boss must
delegate via agent_spawn; worker must call do_task) — semantic
grading alone can't distinguish a boss that delegated from one
that did the work himself, as long as both report the same answer.

Fixture shape (`expected_output.assert_invocations`):

    assert_invocations:
      <agent_name>:
        must_call:     [tool_a, tool_b]   # at least one invocation per tool
        must_not_call: [tool_x]           # zero invocations of any tool here

Multiple agent_names live side-by-side. An agent that appears in the
spec but never ran fails its `must_call` rules (no calls happened)
and trivially passes its `must_not_call` rules (no forbidden calls).
Agents that ran but aren't mentioned in the spec are ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Violation:
    agent: str
    kind: str            # "missing_call" | "forbidden_call"
    tool: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.agent}] {self.kind}: {self.tool} — {self.detail}"


def load_invocations(path: Path | str) -> list[dict]:
    """Read the agent CLI's invocations.json. Returns [] on missing /
    malformed — the caller decides whether that should be a hard fail
    (typically yes if `assert_invocations` is non-empty)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    invs = data.get("invocations") or []
    return invs if isinstance(invs, list) else []


def check(
    invocations: list[dict],
    spec: dict[str, dict],
) -> list[Violation]:
    """Validate invocations against the assert_invocations spec.
    Returns one Violation per failed rule (empty list = all rules pass)."""
    if not spec:
        return []
    # Group invocations by agent name → set of tool names called.
    calls_by_agent: dict[str, list[str]] = {}
    for inv in invocations:
        agent = str(inv.get("agent") or "")
        tool = str(inv.get("tool") or "")
        if not agent or not tool:
            continue
        calls_by_agent.setdefault(agent, []).append(tool)

    violations: list[Violation] = []
    for agent_name, rules in (spec or {}).items():
        if not isinstance(rules, dict):
            continue
        called = calls_by_agent.get(agent_name, [])
        called_set = set(called)
        must_call = rules.get("must_call") or []
        must_not_call = rules.get("must_not_call") or []
        for tool in must_call:
            if tool not in called_set:
                violations.append(Violation(
                    agent=agent_name,
                    kind="missing_call",
                    tool=tool,
                    detail=(
                        f"expected at least one invocation; agent called "
                        f"{sorted(called_set) or '(nothing)'}"
                    ),
                ))
        for tool in must_not_call:
            count = called.count(tool)
            if count > 0:
                violations.append(Violation(
                    agent=agent_name,
                    kind="forbidden_call",
                    tool=tool,
                    detail=f"expected zero invocations; saw {count}",
                ))
    return violations


def format_violations(violations: list[Violation]) -> str:
    """One-line-per-violation human-readable text, prefixed with a
    header. Empty string when no violations — callers can `if msg:`
    without special-casing the empty list."""
    if not violations:
        return ""
    lines = ["assert_invocations violations:"]
    lines.extend(f"  - {v}" for v in violations)
    return "\n".join(lines)
