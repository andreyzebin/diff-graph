"""
Behavioral feedback signals (read-only).

Computes real-time signals from agent execution history.
The framework does NOT act on these automatically — they are
available via observe_agents for supervisor agents to react to.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from .sgr import SGREntry


@dataclass
class FeedbackSignals:
    repetition_score: float = 0.0
    progress_score: float = 0.0
    stuck: bool = False


class RepetitionDetector:
    """Detects repetitive tool call patterns in a sliding window."""

    def __init__(self, window: int = 6) -> None:
        self.window = window
        self._history: deque[str] = deque(maxlen=window)

    def update(self, tool_name: str, tool_args: dict) -> float:
        key = f"{tool_name}|{'|'.join(f'{k}={v}' for k, v in sorted(tool_args.items()))}"
        self._history.append(key)
        if len(self._history) < 2:
            return 0.0

        recent = list(self._history)
        counts: dict[str, int] = {}
        for k in recent:
            counts[k] = counts.get(k, 0) + 1
        max_count = max(counts.values())

        if max_count >= 3:
            return min(max_count / self.window, 1.0) * 0.9
        elif max_count >= 2:
            return min(max_count / self.window, 1.0) * 0.6
        return 0.0


class ProgressTracker:
    """Tracks useful work per step."""

    def __init__(self) -> None:
        self._unique_files: set[str] = set()
        self._total_reflects = 0
        self._total_resolved = 0
        self._steps = 0

    def update(self, sgr: Optional[SGREntry], tool_calls: list[dict],
               findings_count: int) -> float:
        self._steps += 1
        for tc in tool_calls:
            name = tc.get("name", "")
            if name in ("read_file", "read_outline"):
                path = tc.get("args", {}).get("path", "")
                if path:
                    self._unique_files.add(path)
        if sgr:
            self._total_reflects += 1
            self._total_resolved += len(sgr.resolved_questions)

        if self._steps == 0:
            return 0.5
        file_rate = min(len(self._unique_files) / max(self._steps, 1), 1.0)
        resolve_rate = (self._total_resolved / max(self._total_reflects, 1)
                        if self._total_reflects > 0 else 0.5)
        return min(file_rate * 0.4 + resolve_rate * 0.4 + (0.2 if findings_count > 0 else 0), 1.0)


class StuckDetector:
    """Combines repetition + progress to detect stuck state."""

    def __init__(self, rep_threshold: float = 0.6, prog_threshold: float = 0.2) -> None:
        self.rep_threshold = rep_threshold
        self.prog_threshold = prog_threshold

    def check(self, rep_score: float, prog_score: float) -> bool:
        return rep_score > self.rep_threshold and prog_score < self.prog_threshold


class FeedbackEngine:
    """Evaluates all signals after each step. Read-only — does not take actions."""

    def __init__(self) -> None:
        self.repetition = RepetitionDetector()
        self.progress = ProgressTracker()
        self.stuck_detector = StuckDetector()
        self.signals = FeedbackSignals()

    def after_step(self, tool_calls: list[dict], sgr: Optional[SGREntry] = None,
                   findings_count: int = 0) -> FeedbackSignals:
        rep = 0.0
        for tc in tool_calls:
            rep = max(rep, self.repetition.update(tc.get("name", ""), tc.get("args", {})))
        prog = self.progress.update(sgr, tool_calls, findings_count)
        stuck = self.stuck_detector.check(rep, prog)
        self.signals = FeedbackSignals(repetition_score=rep, progress_score=prog, stuck=stuck)
        return self.signals
