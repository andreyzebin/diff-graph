"""
Self-Guided Reasoning (SGR) tracker.

Questions/concerns are tracked by ID for stability. The LLM assigns
short IDs (C1, C2, Q1, Q2...) and resolves by ID, not by text.
Text reformulation doesn't break tracking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SGREntry:
    """One reflect() call's data."""
    step: int
    learned: str = ""
    questions_remaining: list[dict] = field(default_factory=list)  # [{id, text}]
    resolved_questions: list[dict] = field(default_factory=list)   # [{id, resolution, summary}]
    confidence: str = "low"
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
    """Tracks the full SGR history for one agent. Questions tracked by ID."""

    def __init__(self, extensions_schema: Optional[dict[str, Any]] = None) -> None:
        self._history: list[SGREntry] = []
        self._extensions_schema = extensions_schema or {}
        self._prev_confidence: Optional[str] = None
        self._staleness: int = 0
        # Active questions by ID
        self._questions: dict[str, dict] = {}  # id -> {id, text, step_opened, age}
        self._resolved: dict[str, dict] = {}   # id -> {id, resolution, summary, step_resolved}
        self._next_auto_id: int = 1

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
        if not self._history:
            return 0.0
        return _CONFIDENCE_MAP.get(self._history[-1].confidence, 0.0)

    @property
    def open_question_count(self) -> int:
        return len(self._questions)

    @property
    def staleness(self) -> int:
        return self._staleness

    @property
    def open_questions(self) -> dict[str, dict]:
        """Active questions by ID."""
        return dict(self._questions)

    @property
    def resolved_questions(self) -> dict[str, dict]:
        """All resolved questions by ID."""
        return dict(self._resolved)

    def record(self, step: int, args: dict) -> SGREntry:
        """Record a reflect() call. Track questions by ID."""
        core_keys = {"learned", "questions_remaining", "resolved_questions",
                     "confidence", "next_action"}
        extensions = {k: v for k, v in args.items() if k not in core_keys}

        # Normalize questions_remaining to [{id, text}]
        raw_remaining = args.get("questions_remaining", [])
        questions_remaining = self._normalize_questions(raw_remaining, step)

        # Normalize resolved_questions to [{id, resolution, summary}]
        raw_resolved = args.get("resolved_questions", [])
        resolved_questions = self._normalize_resolved(raw_resolved, step)

        entry = SGREntry(
            step=step,
            learned=args.get("learned", ""),
            questions_remaining=questions_remaining,
            resolved_questions=resolved_questions,
            confidence=args.get("confidence", "low"),
            next_action=args.get("next_action", ""),
            extensions=extensions,
        )

        # Update internal tracking
        self._process_resolved(resolved_questions, step)
        self._process_remaining(questions_remaining, step)

        # Track staleness
        if entry.confidence != self._prev_confidence:
            self._staleness = 0
            self._prev_confidence = entry.confidence
        else:
            self._staleness += 1

        self._history.append(entry)
        return entry

    def _normalize_questions(self, raw: list, step: int) -> list[dict]:
        """Normalize questions to [{id, text}]. Accept strings or dicts."""
        result = []
        for item in raw:
            if isinstance(item, str):
                # Plain string — try to match existing question by text similarity
                existing_id = self._find_by_text(item)
                if existing_id:
                    result.append({"id": existing_id, "text": item})
                else:
                    qid = f"Q{self._next_auto_id}"
                    self._next_auto_id += 1
                    result.append({"id": qid, "text": item})
            elif isinstance(item, dict):
                qid = item.get("id", "")
                text = item.get("text", "")
                if not qid:
                    existing_id = self._find_by_text(text)
                    if existing_id:
                        qid = existing_id
                    else:
                        qid = f"Q{self._next_auto_id}"
                        self._next_auto_id += 1
                result.append({"id": qid, "text": text})
        return result

    def _normalize_resolved(self, raw: list, step: int) -> list[dict]:
        """Normalize resolved questions. Accept old format (question text) or new (id)."""
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            qid = item.get("id", "")
            question_text = item.get("question", "")
            resolution = item.get("resolution", "answered")
            summary = item.get("summary", "")

            # If no ID, try to match by question text
            if not qid and question_text:
                qid = self._find_by_text(question_text) or ""

            result.append({
                "id": qid,
                "question": question_text,
                "resolution": resolution,
                "summary": summary,
            })
        return result

    def _find_by_text(self, text: str) -> Optional[str]:
        """Find an existing question ID by text similarity (word overlap)."""
        if not text or not self._questions:
            return None
        text_words = set(text.lower().split())
        best_id = None
        best_overlap = 0.0
        for qid, q in self._questions.items():
            q_words = set(q["text"].lower().split())
            if not q_words:
                continue
            overlap = len(text_words & q_words) / max(len(text_words | q_words), 1)
            if overlap > best_overlap and overlap > 0.5:
                best_overlap = overlap
                best_id = qid
        return best_id

    def _process_resolved(self, resolved: list[dict], step: int) -> None:
        """Move resolved questions from active to resolved."""
        for r in resolved:
            qid = r.get("id", "")
            if qid and qid in self._questions:
                self._resolved[qid] = {
                    "id": qid,
                    "text": self._questions[qid]["text"],
                    "resolution": r.get("resolution", "answered"),
                    "summary": r.get("summary", ""),
                    "step_resolved": step,
                }
                del self._questions[qid]
            elif r.get("question"):
                # Try text match for backward compat
                found = self._find_by_text(r["question"])
                if found and found in self._questions:
                    self._resolved[found] = {
                        "id": found,
                        "text": self._questions[found]["text"],
                        "resolution": r.get("resolution", "answered"),
                        "summary": r.get("summary", ""),
                        "step_resolved": step,
                    }
                    del self._questions[found]

    def _process_remaining(self, remaining: list[dict], step: int) -> None:
        """Update active questions. Detect implicit drops."""
        new_ids = {q["id"] for q in remaining}

        # Questions that disappeared without being resolved → implicit drop
        for qid in list(self._questions.keys()):
            if qid not in new_ids and qid not in self._resolved:
                self._resolved[qid] = {
                    "id": qid,
                    "text": self._questions[qid]["text"],
                    "resolution": "dropped",
                    "summary": "(implicit)",
                    "step_resolved": step,
                }
                del self._questions[qid]

        # Update or add remaining questions
        for q in remaining:
            qid = q["id"]
            if qid in self._questions:
                self._questions[qid]["age"] += 1
                self._questions[qid]["text"] = q["text"]  # allow text refinement
            else:
                self._questions[qid] = {
                    "id": qid,
                    "text": q["text"],
                    "step_opened": step,
                    "age": 0,
                }

    def build_reflect_schema(self) -> dict:
        """Return JSON Schema for reflect() with ID-based questions."""
        properties: dict[str, Any] = {
            "learned": {
                "type": "string",
                "description": "Key facts established so far.",
            },
            "questions_remaining": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string",
                                "description": "Short stable ID (Q1, Q2...). Reuse same ID across reflects."},
                        "text": {"type": "string",
                                 "description": "The question text."},
                    },
                    "required": ["id", "text"],
                },
                "description": (
                    "Questions you still need to investigate. Only list things you "
                    "DON'T know yet. If you already have the answer, put it in 'learned' instead."
                ),
            },
            "resolved_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string",
                                "description": "ID of the question from your PREVIOUS reflect."},
                        "resolution": {"type": "string", "enum": ["answered", "dropped"]},
                        "summary": {"type": "string",
                                    "description": "The concrete answer you found, or reason for dropping."},
                    },
                    "required": ["id", "resolution", "summary"],
                },
                "description": (
                    "Questions from your PREVIOUS reflect() that you can now answer. "
                    "Reference by ID. Include the answer in summary. "
                    "Do NOT resolve questions you just opened in this same reflect."
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
        if not self._history:
            return []
        if mode == "last":
            return [self._history[-1].to_dict()]
        if mode == "first_and_last":
            entries = [self._history[0]]
            if len(self._history) > 1:
                entries.append(self._history[-1])
            return [e.to_dict() for e in entries]
        return [e.to_dict() for e in self._history]
