"""Semantic mock matching — pick the right fixture for a tool call by
asking an LLM "which of these N pre-authored fixtures matches the
intent of this call?" Mockito with AI taste.

Why this exists:
- Strict-ordinal mocks (orchestra/tool_mocks.py default) don't know
  the focus of an agent_spawn call. The reviewer's i-th investigator
  focus might be about ownership, but the i-th canned response might
  be about pricing — reviewer gets a non-sequitur and the synthesis
  test fails for the wrong reason.
- capture_only mocks return a fixed stub that the agent recognises
  as "not a real answer" and falls back to direct reading — leak.
- Semantic matching closes both gaps: pre-author investigation
  summaries keyed by the *intent* of the focus, then route at run
  time by asking a model which fixture best fits the actual focus.
  Reviewer sees a believable child summary, synthesis is testable.

Strategy interface:
- `llm_judge` (this file, v1) — single LLM call, list all fixtures,
  ask for a 1-N pick + confidence.
- `embedding` (future, plug-in) — sentence-transformers cosine sim
  over pre-embedded fixture examples. Local, no network, ~50ms.
- `regex` / `keyword` (future, plug-in) — for cases where intent
  is dead-simple to express literally.

All strategies return a `MatchResult` with the same shape so the
mock dispatcher can pick the policy uniformly (threshold, no-match
fallback).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


@dataclass
class SemanticFixture:
    """One pre-authored response keyed by a list of intent examples.
    The runtime picks fixtures by matching the agent's actual call
    against `examples` — any phrasing close enough wins."""
    examples: list[str]
    return_data: Any


@dataclass
class SemanticConfig:
    """Per-tool semantic-mock config. Lives alongside (not instead
    of) the ordinal `by_tool` entries — a single ToolMocks can have
    some tools mocked ordinally and others semantically."""
    strategy: str               # "llm_judge"; embeddings/etc. plug in here
    fixtures: list[SemanticFixture]
    model: Optional[str] = None  # None → use mock's default model
    on_arg: str = "focus"        # which arg field carries the intent text
    threshold: float = 0.6       # min confidence to accept the match
    # When the strategy returns no match (or under threshold), what
    # do we hand back to the agent?
    #   "stub"  — same neutral stub capture_only would return
    #             (the test still captures the call's args)
    #   "error" — raise MockExhaustedError; surfaces as a test
    #             failure rather than silent fallback
    on_no_match: str = "stub"


@dataclass
class MatchResult:
    """Strategy output. `index = -1` means no fixture matched (or
    the strategy couldn't decide)."""
    index: int
    confidence: float
    reason: str


# Default neutral stub — same shape capture_only uses, kept here so
# the no-match fallback path is honest about being a fallback rather
# than a real investigation result.
NO_MATCH_STUB = (
    '{"status":"spawned","child_id":"<no-match-stub>",'
    '"mode":"semantic","note":"no fixture matched this focus"}'
)


def match(
    actual: str,
    cfg: SemanticConfig,
    *,
    llm_caller: Optional[Callable[..., str]] = None,
) -> MatchResult:
    """Dispatch to the named strategy. Adding a new strategy =
    one more `elif` here + the impl module. Strategy implementations
    don't import this module — keeps the dependency arrow pointing
    inward (tool_mocks → semantic → strategy impl)."""
    if not cfg.fixtures:
        return MatchResult(-1, 0.0, "no fixtures configured")
    if cfg.strategy == "llm_judge":
        if llm_caller is None:
            raise RuntimeError(
                "semantic match strategy 'llm_judge' requires an "
                "llm_caller to be injected on ToolMocks "
                "(see ToolMocks.set_llm_caller)"
            )
        return llm_judge_match(
            actual, cfg.fixtures, model=cfg.model, llm_caller=llm_caller,
        )
    raise ValueError(
        f"unknown semantic match strategy: {cfg.strategy!r}. "
        f"Supported: 'llm_judge'."
    )


# ── LLM judge strategy ─────────────────────────────────────────────


_PROMPT = """You are matching an investigation focus to one of {n} pre-authored fixtures.

Each fixture has example focus phrasings — pick the fixture whose examples describe the SAME concern as the actual focus below. Phrasing will differ; intent is what matters.

ACTUAL FOCUS:
{actual!r}

CANDIDATES:
{candidates}

Return JSON only (no prose, no code fence):
{{"match": <integer 1-{n} for a match, or 0 for no match>, "confidence": <float 0.0-1.0>, "reason": "<one short line, max 80 chars>"}}"""


def llm_judge_match(
    actual: str,
    fixtures: list[SemanticFixture],
    *,
    model: Optional[str],
    llm_caller: Callable[..., str],
) -> MatchResult:
    """Single-shot LLM call. Enumerates all candidates so the model
    sees them at once and picks the best one. Cheap (one call per
    matched mock dispatch) and provider-agnostic — same code paths
    work for deepseek, openai, anthropic etc."""
    blocks = []
    for i, f in enumerate(fixtures, 1):
        bullets = "\n".join(f"  - {ex}" for ex in f.examples)
        blocks.append(f"[{i}]\n{bullets}")
    candidates_block = "\n\n".join(blocks)

    prompt = _PROMPT.format(
        n=len(fixtures), actual=actual, candidates=candidates_block,
    )
    raw = llm_caller([{"role": "user", "content": prompt}], model=model)
    parsed = _parse_judge_json(raw)
    if parsed is None:
        log.warning("llm_judge: unparseable response %r", raw[:120])
        return MatchResult(-1, 0.0, "judge returned unparseable JSON")

    try:
        match_idx = int(parsed.get("match", 0))
    except (TypeError, ValueError):
        match_idx = 0
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason", ""))[:200]

    if match_idx < 1 or match_idx > len(fixtures):
        return MatchResult(-1, confidence, reason or "out-of-range match index")
    return MatchResult(match_idx - 1, confidence, reason)


# Models sometimes wrap JSON in code fences or add prose; try the
# straightforward parse first, then fall back to extracting the
# first {...} blob.
_JSON_BLOB = re.compile(r"\{.*?\}", re.DOTALL)


def _parse_judge_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    # Strip code fences if present
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Fallback: first JSON-looking blob
    m = _JSON_BLOB.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None
