from __future__ import annotations
import logging
from typing import Optional

from .extractor import OnEvent
from .prompts import load as _load_prompt

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = _load_prompt("planner_system.txt")


def plan_review(changed_block: str, llm, model: str,
                on_event: Optional[OnEvent] = None) -> str:
    """
    Single cheap LLM call: analyze the changed symbols and return a review strategy string.
    The strategy is injected into the review agent's initial user message.

    Returns an empty string on failure (review agent proceeds without strategy hint).
    """
    _emit = on_event or (lambda *_, **__: None)
    if not changed_block.strip():
        return ""

    try:
        response = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": changed_block},
            ],
            temperature=0,
            max_tokens=300,
        )
        strategy = response.choices[0].message.content or ""
        log.debug("planner strategy: %s", strategy[:120])
        return strategy.strip()
    except Exception as exc:
        log.warning("planner failed: %s", exc)
        return ""
