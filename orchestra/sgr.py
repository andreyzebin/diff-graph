"""
Self-Guided Reasoning (SGR) tracker.

Maintains the full history of reflect() calls for an agent, supports
custom extension fields, and extracts data for handoff/observability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SGREntry:
    """One reflect() call's data."""
    step: int
    learned: str = ""
    questions_remaining: list[str] = field(default_factory=list)
    resolved_questions: list[dict[str, str]] = field(default_factory=list)
    confidence: str = "low"  # low | medium | high
    next_action: str = ""
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "learned": self.learned,
            "questions_remaining": self.questions_remaining,
            "resolved_questions": self.resolved_questions,
            "confidence": self.confidence,
            "next_action": self.next_action,
        }
        d.update(self.extensions)
        return d


_CONFIDENCE_MAP = {"low": 0.0, "medium": 0.5, "high": 1.0}


class SGRTracker:
    """Tracks the full SGR history for one agent."""

    def __init__(self, extensions_schema: Optional[dict[str, Any]] = None) -> None:
        self._history: list[SGREntry] = []
        self._extensions_schema = extensions_schema or {}
        self._prev_confidence: Optional[str] = None
        self._staleness: int = 0

    @property
    def history(self) -> list[SGREntry]:
        return list(self._history)

    @property
    def last(self) -> Optional[SGREntry]:
        return self._history[-1] if self._history else None

    @property
    def count(self) -> int:
        return len(self._history)

    @property
    def confidence_numeric(self) -> float:
        """low=0.0, medium=0.5, high=1.0."""
        if not self._history:
            return 0.0
        return _CONFIDENCE_MAP.get(self._history[-1].confidence, 0.0)

    @property
    def open_question_count(self) -> int:
        if not self._history:
            return 0
        return len(self._history[-1].questions_remaining)

    @property
    def staleness(self) -> int:
        """Number of reflect() calls since confidence last changed."""
        return self._staleness

    def record(self, step: int, args: dict) -> SGREntry:
        """Record a reflect() call and return the entry."""
        # Separate extension fields from core fields
        core_keys = {"learned", "questions_remaining", "resolved_questions",
                     "confidence", "next_action"}
        extensions = {k: v for k, v in args.items() if k not in core_keys}

        entry = SGREntry(
            step=step,
            learned=args.get("learned", ""),
            questions_remaining=args.get("questions_remaining", []),
            resolved_questions=args.get("resolved_questions", []),
            confidence=args.get("confidence", "low"),
            next_action=args.get("next_action", ""),
            extensions=extensions,
        )

        # Track staleness
        if entry.confidence != self._prev_confidence:
            self._staleness = 0
            self._prev_confidence = entry.confidence
        else:
            self._staleness += 1

        self._history.append(entry)
        return entry

    def build_reflect_schema(self) -> dict:
        """Return JSON Schema for the reflect() tool, including extensions."""
        properties: dict[str, Any] = {
            "learned": {
                "type": "string",
                "description": "Key facts established so far.",
            },
            "questions_remaining": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Open questions still to answer.",
            },
            "resolved_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "resolution": {"type": "string", "enum": ["answered", "dropped"]},
                        "summary": {"type": "string",
                                    "description": "The answer, or reason for dropping."},
                    },
                    "required": ["question", "resolution", "summary"],
                },
                "description": (
                    "Questions from the previous reflect() that are now resolved. "
                    "Move each question here as 'answered' (with the answer) or "
                    "'dropped' (with reason). Do not leave questions open indefinitely."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Confidence in current findings.",
            },
            "next_action": {
                "type": "string",
                "description": "What to do next and why.",
            },
        }
        required = ["learned", "questions_remaining", "confidence", "next_action"]

        # Merge extensions
        for ext_name, ext_schema in self._extensions_schema.items():
            properties[ext_name] = ext_schema
            if ext_schema.get("required"):
                required.append(ext_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def extract_for_handoff(self, mode: str) -> list[dict]:
        """
        Extract SGR data for context handoff.

        Modes:
          - "last"           → only the last reflect()
          - "all"            → all reflect() calls
          - "first_and_last" → first and last
        """
        if not self._history:
            return []
        if mode == "last":
            return [self._history[-1].to_dict()]
        if mode == "first_and_last":
            entries = [self._history[0]]
            if len(self._history) > 1:
                entries.append(self._history[-1])
            return [e.to_dict() for e in entries]
        # Default: all
        return [e.to_dict() for e in self._history]
