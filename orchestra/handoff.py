"""
Context handoff modes.

When one agent passes context to another, the handoff mode controls
what is transferred: full message history, SGR data, findings, condensed
summary, or a custom combination.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Protocol

from .sgr import SGREntry

log = logging.getLogger(__name__)


class HandoffMode(Protocol):
    def apply(
        self,
        messages: list[dict],
        sgr_history: list[SGREntry],
        output: Any,
        llm: Any = None,
        model: str = "",
    ) -> list[dict]: ...


class FullHistoryHandoff:
    """Fork with complete message history."""
    def apply(self, messages, sgr_history, output, llm=None, model=""):
        return list(messages)


class SGROutcomesHandoff:
    """Only the last reflect() output."""
    def apply(self, messages, sgr_history, output, llm=None, model=""):
        if not sgr_history:
            return []
        last = sgr_history[-1]
        content = f"[Previous agent's final reflection]\n{json.dumps(last.to_dict(), indent=2)}"
        return [{"role": "user", "content": content}]


class AllSGRHandoff:
    """All reflect() calls in order."""
    def apply(self, messages, sgr_history, output, llm=None, model=""):
        if not sgr_history:
            return []
        entries = [e.to_dict() for e in sgr_history]
        content = f"[Previous agent's reasoning trajectory]\n{json.dumps(entries, indent=2)}"
        return [{"role": "user", "content": content}]


class FindingsOnlyHandoff:
    """Only the done() output, no history."""
    def apply(self, messages, sgr_history, output, llm=None, model=""):
        if output is None:
            return []
        content = f"[Previous agent's output]\n{json.dumps(output, indent=2, ensure_ascii=False)}"
        return [{"role": "user", "content": content}]


class FindingsAndSGRHandoff:
    """done() output + all SGR calls."""
    def apply(self, messages, sgr_history, output, llm=None, model=""):
        parts = []
        if sgr_history:
            entries = [e.to_dict() for e in sgr_history]
            parts.append(f"[Reasoning trajectory]\n{json.dumps(entries, indent=2)}")
        if output is not None:
            parts.append(f"[Output]\n{json.dumps(output, indent=2, ensure_ascii=False)}")
        if not parts:
            return []
        return [{"role": "user", "content": "\n\n".join(parts)}]


class CondensedHandoff:
    """LLM-summarized history."""
    def __init__(self, condense_prompt: str = "Summarize the investigation so far in <500 words."):
        self._prompt = condense_prompt

    def apply(self, messages, sgr_history, output, llm=None, model=""):
        if not messages or llm is None:
            return FindingsOnlyHandoff().apply(messages, sgr_history, output)

        # Build text to summarize
        text_parts = []
        for m in messages:
            if m.get("role") == "system":
                continue
            content = m.get("content", "")
            if content:
                text_parts.append(f"[{m['role']}] {content[:500]}")
        full_text = "\n".join(text_parts)

        try:
            resp = llm.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": self._prompt},
                    {"role": "user", "content": full_text[:8000]},
                ],
                temperature=0,
                stream=False,
            )
            summary = resp.choices[0].message.content or ""
        except Exception as e:
            log.warning("condensed handoff failed: %s", e)
            summary = full_text[:2000]

        return [{"role": "user", "content": f"[Condensed context from previous agent]\n{summary}"}]


class LastNHandoff:
    """Last N messages, optionally with SGR bookends."""
    def __init__(self, n: int = 20, include_sgr: str = ""):
        self._n = n
        self._include_sgr = include_sgr  # "first_and_last", "last", ""

    def apply(self, messages, sgr_history, output, llm=None, model=""):
        result = []

        # SGR prefix
        if self._include_sgr and sgr_history:
            if self._include_sgr == "first_and_last":
                entries = [sgr_history[0].to_dict()]
                if len(sgr_history) > 1:
                    entries.append(sgr_history[-1].to_dict())
            elif self._include_sgr == "last":
                entries = [sgr_history[-1].to_dict()]
            else:
                entries = [e.to_dict() for e in sgr_history]
            result.append({
                "role": "user",
                "content": f"[SGR context]\n{json.dumps(entries, indent=2)}",
            })

        # Last N messages (skip system)
        body = [m for m in messages if m.get("role") != "system"]
        result.extend(body[-self._n:])
        return result


class CustomHandoff:
    """User-provided Python callable."""
    def __init__(self, handler: Callable):
        self._handler = handler

    def apply(self, messages, sgr_history, output, llm=None, model=""):
        return self._handler(messages, sgr_history, output)


# ── Factory ───────────────────────────────────────────────────────────────────

_MODES: dict[str, type] = {
    "full_history": FullHistoryHandoff,
    "sgr_outcomes": SGROutcomesHandoff,
    "all_sgr": AllSGRHandoff,
    "findings_only": FindingsOnlyHandoff,
    "findings_and_sgr": FindingsAndSGRHandoff,
    "condensed": CondensedHandoff,
}


def get_handoff(mode: str, **kwargs: Any) -> HandoffMode:
    """Factory: string name -> HandoffMode instance."""
    if mode.startswith("last_"):
        try:
            n = int(mode.split("_")[1])
        except (IndexError, ValueError):
            n = 20
        return LastNHandoff(n=n, **kwargs)

    cls = _MODES.get(mode)
    if cls is None:
        log.warning("unknown handoff mode '%s', falling back to findings_only", mode)
        return FindingsOnlyHandoff()
    return cls(**kwargs)


def compose_handoff(
    includes: list[str],
    messages: list[dict],
    sgr_history: list[SGREntry],
    output: Any,
) -> list[dict]:
    """
    Compose multiple include directives into a single message list.

    Directives: "output", "last_sgr", "all_sgr", "first_sgr",
                "last_N_messages" (e.g. "last_5_messages").
    """
    result: list[dict] = []
    for inc in includes:
        if inc == "output" and output is not None:
            result.append({
                "role": "user",
                "content": f"[Output]\n{json.dumps(output, indent=2, ensure_ascii=False)}",
            })
        elif inc == "last_sgr" and sgr_history:
            result.append({
                "role": "user",
                "content": f"[Last reflection]\n{json.dumps(sgr_history[-1].to_dict(), indent=2)}",
            })
        elif inc == "all_sgr" and sgr_history:
            entries = [e.to_dict() for e in sgr_history]
            result.append({
                "role": "user",
                "content": f"[All reflections]\n{json.dumps(entries, indent=2)}",
            })
        elif inc == "first_sgr" and sgr_history:
            result.append({
                "role": "user",
                "content": f"[First reflection]\n{json.dumps(sgr_history[0].to_dict(), indent=2)}",
            })
        elif inc.startswith("last_") and inc.endswith("_messages"):
            try:
                n = int(inc.split("_")[1])
            except (IndexError, ValueError):
                n = 5
            body = [m for m in messages if m.get("role") != "system"]
            result.extend(body[-n:])
    return result
