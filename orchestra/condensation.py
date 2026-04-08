"""
Message condensation strategies for long-running agents.

When an agent's message history grows beyond a token threshold, a condenser
replaces older messages with a compressed representation while preserving
the system message, recent messages, and (optionally) SGR reflect() calls.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Protocol

from .types import CondensationConfig, CondensationStrategy
from .sgr import SGREntry

log = logging.getLogger(__name__)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        # tool call arguments
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            total += len(fn.get("arguments", ""))
    return total // 4


def should_condense(messages: list[dict], threshold: int) -> bool:
    return estimate_tokens(messages) > threshold


class Condenser(Protocol):
    def condense(
        self,
        messages: list[dict],
        config: CondensationConfig,
        sgr_entries: list[SGREntry],
        llm: Any,
        model: str,
    ) -> list[dict]: ...


class LLMSummaryCondenser:
    """Replace old messages with an LLM-generated summary."""

    def condense(self, messages: list[dict], config: CondensationConfig,
                 sgr_entries: list[SGREntry], llm: Any, model: str) -> list[dict]:
        if len(messages) <= config.preserve_last + 1:
            return messages

        system = messages[0] if messages and messages[0].get("role") == "system" else None
        body = messages[1:] if system else messages

        keep_count = config.preserve_last
        old_messages = body[:-keep_count] if keep_count > 0 else body
        recent = body[-keep_count:] if keep_count > 0 else []

        # Build summary of old messages
        old_text = _messages_to_text(old_messages)

        # Optionally preserve SGR entries
        sgr_text = ""
        if config.preserve_sgr and sgr_entries:
            sgr_text = "\n\nSGR HISTORY:\n" + "\n".join(
                f"- [confidence={e.confidence}] {e.learned}" for e in sgr_entries
            )

        try:
            resp = llm.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": config.condense_prompt},
                    {"role": "user", "content": old_text + sgr_text},
                ],
                temperature=0,
                stream=False,
            )
            summary = resp.choices[0].message.content or ""
        except Exception as e:
            log.warning("LLM condensation failed: %s", e)
            summary = old_text[:2000] + "\n... (condensation failed, truncated)"

        result = []
        if system:
            result.append(system)
        result.append({"role": "user", "content": f"[Condensed history]\n{summary}"})
        result.extend(recent)
        return result


class SlidingWindowCondenser:
    """Keep only last N messages, prepend a summary header."""

    def condense(self, messages: list[dict], config: CondensationConfig,
                 sgr_entries: list[SGREntry], llm: Any, model: str) -> list[dict]:
        system = messages[0] if messages and messages[0].get("role") == "system" else None
        body = messages[1:] if system else messages

        keep_count = config.preserve_last
        dropped = body[:-keep_count] if keep_count > 0 and len(body) > keep_count else []
        recent = body[-keep_count:] if keep_count > 0 else body

        result = []
        if system:
            result.append(system)

        if dropped:
            n_steps = sum(1 for m in dropped if m.get("role") == "assistant")
            header = f"[{len(dropped)} earlier messages condensed ({n_steps} steps)]"
            if config.preserve_sgr and sgr_entries:
                header += "\nLatest SGR: " + (sgr_entries[-1].learned[:200] if sgr_entries else "")
            result.append({"role": "user", "content": header})

        result.extend(recent)
        return result


class DropToolResultsCondenser:
    """Keep tool calls but truncate results to first N chars."""

    def condense(self, messages: list[dict], config: CondensationConfig,
                 sgr_entries: list[SGREntry], llm: Any, model: str) -> list[dict]:
        keep_count = config.preserve_last
        result = []

        for i, m in enumerate(messages):
            # Always keep system, and last N messages intact
            if i == 0 and m.get("role") == "system":
                result.append(m)
                continue
            if i >= len(messages) - keep_count:
                result.append(m)
                continue

            if m.get("role") == "tool":
                content = m.get("content", "")
                if len(content) > 200:
                    m = dict(m)
                    m["content"] = content[:200] + "... (condensed)"
            result.append(m)

        return result


class HybridCondenser:
    """LLM-summarize old messages, keep recent verbatim."""

    def condense(self, messages: list[dict], config: CondensationConfig,
                 sgr_entries: list[SGREntry], llm: Any, model: str) -> list[dict]:
        # First pass: drop tool results on old messages
        drop = DropToolResultsCondenser()
        intermediate = drop.condense(messages, config, sgr_entries, llm, model)

        # If still over threshold, do LLM summary
        if should_condense(intermediate, config.trigger):
            llm_cond = LLMSummaryCondenser()
            return llm_cond.condense(intermediate, config, sgr_entries, llm, model)

        return intermediate


def get_condenser(strategy: CondensationStrategy) -> Condenser:
    """Factory: strategy enum → Condenser instance."""
    return {
        CondensationStrategy.LLM_SUMMARY: LLMSummaryCondenser,
        CondensationStrategy.SLIDING_WINDOW: SlidingWindowCondenser,
        CondensationStrategy.DROP_TOOL_RESULTS: DropToolResultsCondenser,
        CondensationStrategy.HYBRID: HybridCondenser,
    }[strategy]()


def _messages_to_text(messages: list[dict]) -> str:
    """Flatten messages to plain text for summarization."""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                calls.append(f"{fn.get('name', '?')}({fn.get('arguments', '')[:100]})")
            content = "Tool calls: " + ", ".join(calls)
        if content:
            parts.append(f"[{role}] {content[:500]}")
    return "\n".join(parts)
