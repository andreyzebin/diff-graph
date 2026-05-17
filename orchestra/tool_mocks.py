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
from typing import Any, Callable, Optional

from .tool_mocks_semantic import (
    SemanticConfig,
    SemanticFixture,
    NO_MATCH_STUB,
    match as semantic_match,
)

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
    # Per-tool semantic config (separate from `by_tool` ordinal
    # entries). A given tool sits in exactly one of the two maps.
    semantic_by_tool: dict[str, SemanticConfig] = field(default_factory=dict)
    source_path: str = ""
    # Per-tool set of consumed entry indices. Each entry is consumed
    # at most once over the whole run.
    _consumed: dict[str, set] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Optional LLM caller for semantic strategies that need a model
    # round-trip (set after construction by cli.py / run_unit).
    # Signature: (messages: list[dict], *, model: Optional[str]) -> str.
    _llm_caller: Optional[Callable[..., str]] = field(default=None, repr=False)

    def has(self, tool_name: str) -> bool:
        return tool_name in self.by_tool or tool_name in self.semantic_by_tool

    def set_llm_caller(self, caller: Callable[..., str]) -> None:
        """Inject the LLM round-trip the semantic strategies use.
        Always safe to call — strategies that don't need an LLM
        ignore it. The caller takes `messages` and a `model=`
        keyword (None ⇒ fall back to its own default)."""
        self._llm_caller = caller

    def consume(self, tool_name: str, args: dict) -> MockEntry:
        """Take the next ordinal entry for this tool — i-th call to
        the tool consumes the i-th entry, regardless of args.

        Why strict ordinal (and no arg matching):
        - The agent's free-text focus is LLM-shaped and varies between
          runs. Pinning the canned response to a focus substring made
          tests brittle.
        - The judge handles focus verification separately by reading
          the invocations.json file (Mockito-style verify).

        Semantic-mode tools take a different path here — the actual
        arg (default `focus`) is routed through a strategy
        (LLM-judge / embeddings / regex) which picks the best
        fixture. See orchestra/tool_mocks_semantic.py.

        Raises MockExhaustedError when the agent makes more calls than
        the fixture lists.
        """
        if tool_name in self.semantic_by_tool:
            return self._consume_semantic(tool_name, args)
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

    def _consume_semantic(self, tool_name: str, args: dict) -> MockEntry:
        """Semantic-mock dispatch — run the configured strategy
        against the actual call args, return the matched fixture
        as a synthetic MockEntry so the rest of the dispatch path
        (render_mock_result, event emit) stays unchanged.

        Threshold + no-match policies live on the SemanticConfig.
        On no-match → return a neutral stub (default) or raise
        MockExhaustedError (`on_no_match: error`)."""
        cfg = self.semantic_by_tool[tool_name]
        actual = str(args.get(cfg.on_arg, ""))
        result = semantic_match(actual, cfg, llm_caller=self._llm_caller)
        log.info(
            "semantic_match: tool=%s arg=%s match=%d conf=%.2f reason=%s",
            tool_name, cfg.on_arg, result.index, result.confidence,
            result.reason,
        )
        if result.index >= 0 and result.confidence >= cfg.threshold:
            fixture = cfg.fixtures[result.index]
            return MockEntry(
                when={}, return_data=fixture.return_data, sticky=True,
            )
        # No-match policy
        if cfg.on_no_match == "error":
            raise MockExhaustedError(
                f"semantic match failed for {tool_name!r} (actual={actual!r}, "
                f"best={result.index} conf={result.confidence:.2f} "
                f"<threshold {cfg.threshold}); reason={result.reason!r}. "
                f"Either lower threshold, add a fixture, or change "
                f"on_no_match to 'stub'."
            )
        return MockEntry(when={}, return_data=NO_MATCH_STUB, sticky=True)

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "ToolMocks":
        by_tool: dict[str, list[MockEntry]] = {}
        _semantic_by_tool: dict[str, SemanticConfig] = {}
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
            # `{mode: capture_only}` — delegation-isolation preset for
            # agent_spawn (TODO §13.6). The tool returns a fixed
            # neutral stub per call (identical regardless of focus) so
            # the agent treats every spawn as "delegated, OK" without
            # any mocked investigator content leaking back into its
            # reasoning chain. Args are still captured in
            # invocations.json (the agent layer records every dispatch
            # before the mock intercepts), so the judge can verify
            # which agents/focuses were delegated. Unlimited replays
            # — sticky.
            if isinstance(raw_entries, dict) and raw_entries.get("mode") == "capture_only":
                by_tool[tool_name] = [MockEntry(
                    when={},
                    return_data='{"status":"spawned","child_id":"<test-stub>","mode":"capture_only"}',
                    sticky=True,
                )]
                continue
            # `{mode: semantic, match: {...}, fixtures: [...]}` —
            # intent-routed mock: each fixture lists example focus
            # phrasings; the chosen strategy (LLM-judge by default)
            # picks the closest fixture at dispatch time. See
            # orchestra/tool_mocks_semantic.py for the strategy
            # interface + LLM-judge prompt. Lives on the SEPARATE
            # semantic_by_tool map so it doesn't shadow / share state
            # with ordinal fixtures for the same tool.
            if isinstance(raw_entries, dict) and raw_entries.get("mode") == "semantic":
                _semantic_by_tool[tool_name] = _parse_semantic_block(
                    tool_name, raw_entries,
                )
                continue
            if not isinstance(raw_entries, list):
                raise ValueError(
                    f"mocks for tool '{tool_name}' must be a list, string, "
                    f"{{mode: capture_only}}, or {{mode: semantic, ...}}; "
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
        return cls(
            by_tool=by_tool,
            semantic_by_tool=_semantic_by_tool,
            source_path=source_path,
        )

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


def _parse_semantic_block(tool_name: str, raw: dict) -> SemanticConfig:
    """Parse a `{mode: semantic, match: {...}, fixtures: [...]}`
    block into a SemanticConfig. Validation here surfaces YAML
    typos at fixture-load time rather than at first mock dispatch
    (which is mid-bench-run and harder to debug)."""
    match_spec = raw.get("match") or {}
    if not isinstance(match_spec, dict):
        raise ValueError(
            f"mocks/{tool_name}: `match` must be a mapping, "
            f"got {type(match_spec).__name__}"
        )
    raw_fixtures = raw.get("fixtures") or []
    if not isinstance(raw_fixtures, list) or not raw_fixtures:
        raise ValueError(
            f"mocks/{tool_name}: `fixtures` must be a non-empty list"
        )
    fixtures: list[SemanticFixture] = []
    for i, fx in enumerate(raw_fixtures):
        if not isinstance(fx, dict):
            raise ValueError(
                f"mocks/{tool_name}.fixtures[{i}] must be a mapping"
            )
        examples = fx.get("examples") or []
        if not isinstance(examples, list) or not examples:
            raise ValueError(
                f"mocks/{tool_name}.fixtures[{i}].examples must be a "
                f"non-empty list of strings"
            )
        if "return" not in fx:
            raise ValueError(
                f"mocks/{tool_name}.fixtures[{i}] missing 'return'"
            )
        fixtures.append(SemanticFixture(
            examples=[str(e) for e in examples],
            return_data=fx["return"],
        ))
    strategy = str(match_spec.get("strategy", "llm_judge"))
    on_no_match = str(match_spec.get("on_no_match", "stub"))
    if on_no_match not in ("stub", "error"):
        raise ValueError(
            f"mocks/{tool_name}.match.on_no_match must be 'stub' or "
            f"'error', got {on_no_match!r}"
        )
    return SemanticConfig(
        strategy=strategy,
        fixtures=fixtures,
        model=match_spec.get("model"),
        on_arg=str(match_spec.get("on_arg", "focus")),
        threshold=float(match_spec.get("threshold", 0.6)),
        on_no_match=on_no_match,
    )


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

    Accepted shapes for an agent_spawn mock's `return_data`:
      - dict with `output:` / `findings:` / `confidence:` / etc.
        (legacy structured form — ordinal-mode fixtures).
      - bare string (semantic-mode fixtures + capture_only stub
        + any "the investigator wrote this back" form). Used
        verbatim as the envelope's `output` field — the agent
        sees it the same way it would see a real investigator's
        done() summary text.
    """
    if tool_name != "agent_spawn":
        return entry.return_data

    # Bare-string fast path — covers semantic fixtures and
    # capture_only's stub (which is a JSON string). Without this
    # the envelope-builder's "else: json.dumps({})" branch
    # silently swallows the canned text into `"output": "{}"`,
    # which is exactly the regression that hid semantic-match
    # responses from the reviewer on REV-U-008 plan 284.
    if isinstance(entry.return_data, str):
        return json.dumps({
            "status": "completed",
            "agent_id": "mock",
            "agent_name": str(args.get("agent", "")),
            "output": entry.return_data,
            "sgr_summary": "",
            "steps": 0,
            "tokens": 0,
            "mocked": True,
        }, ensure_ascii=False, indent=2, default=str)

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
