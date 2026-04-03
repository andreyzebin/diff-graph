from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .extractor import OnEvent
from .prompts import load as _load_prompt

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = _load_prompt("reviewer_system.txt")


@dataclass
class ReviewComment:
    file: str
    line: int
    severity: str   # "BLOCKER" | "NORMAL"
    comment: str
    suggestion: Optional[str] = None  # single replacement line, or None


def generate_review_comments(
    context: str,
    llm,
    model: str,
    on_event: Optional[OnEvent] = None,
) -> list[ReviewComment]:
    """
    One LLM call: take the rendered context → return structured review comments.
    """
    _emit = on_event or (lambda *_, **__: None)

    _emit("reviewer_start")
    try:
        response = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0,
        )
    except Exception as exc:
        log.warning("reviewer LLM call failed: %s", exc)
        _emit("reviewer_failed", reason=str(exc))
        return []

    raw = response.choices[0].message.content or ""
    _emit("reviewer_done",
          tok_in=response.usage.prompt_tokens if response.usage else 0,
          tok_out=response.usage.completion_tokens if response.usage else 0)

    return _parse_comments(raw)


def _parse_comments(raw: str) -> list[ReviewComment]:
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("reviewer: failed to parse JSON: %s\n%s", exc, text[:300])
        return []

    if not isinstance(items, list):
        log.warning("reviewer: expected list, got %s", type(items))
        return []

    comments: list[ReviewComment] = []
    for item in items:
        try:
            comments.append(ReviewComment(
                file=item["file"],
                line=int(item["line"]),
                severity=item.get("severity", "NORMAL").upper(),
                comment=item["comment"],
                suggestion=item.get("suggestion") or None,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("reviewer: bad comment item %s: %s", item, exc)
    return comments
