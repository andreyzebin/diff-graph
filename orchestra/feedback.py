"""
Behavioral feedback signals.

Computes real-time signals from agent execution history:
  - repetition_score: detects tool call loops
  - progress_score: tracks useful work per step
  - stuck_detector: combines signals to detect unproductive loops
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from .sgr import SGREntry
from .events import EventBus, EventType

log = logging.getLogger(__name__)


@dataclass
class FeedbackSignals:
    """Computed behavioral signals for the current step."""
    repetition_score: float = 0.0
    progress_score: float = 0.0
    stuck: bool = False


class RepetitionDetector:
    """Detects repetitive tool call patterns in a sliding window."""

    def __init__(self, window: int = 6, config: Optional[dict] = None) -> None:
        self.window = window
        self._history: deque[tuple[str, str]] = deque(maxlen=window)  # (tool_name, args_hash)
        self._config = config or {}
        # Configurable weights
        self._same_tool_weight = 0.5
        self._same_args_weight = 0.8
        self._same_file_weight = 0.9

        if config:
            for trigger in config.get("triggers", []):
                cond = trigger.get("condition", "")
                weight = trigger.get("weight", 0.5)
                if "same_tool" in cond:
                    self._same_tool_weight = weight
                elif "same_args" in cond:
                    self._same_args_weight = weight
                elif "same_file" in cond:
                    self._same_file_weight = weight

    def update(self, tool_name: str, tool_args: dict) -> float:
        """Record a tool call and return updated repetition score."""
        args_key = _args_hash(tool_name, tool_args)
        self._history.append((tool_name, args_key))

        if len(self._history) < 2:
            return 0.0

        score = 0.0
        recent = list(self._history)

        # Same tool called 3+ times in window
        tool_counts: dict[str, int] = {}
        for tn, _ in recent:
            tool_counts[tn] = tool_counts.get(tn, 0) + 1
        max_tool = max(tool_counts.values())
        if max_tool >= 3:
            score = max(score, self._same_tool_weight * (max_tool / self.window))

        # Same tool+args called 2+ times
        args_counts: dict[str, int] = {}
        for _, ak in recent:
            args_counts[ak] = args_counts.get(ak, 0) + 1
        max_args = max(args_counts.values()) if args_counts else 0
        if max_args >= 2:
            score = max(score, self._same_args_weight * (max_args / self.window))

        # Same file read 3+ times
        file_reads: dict[str, int] = {}
        for tn, ak in recent:
            if tn in ("read_file", "read_outline"):
                file_reads[ak] = file_reads.get(ak, 0) + 1
        if file_reads:
            max_file = max(file_reads.values())
            if max_file >= 3:
                score = max(score, self._same_file_weight * (max_file / self.window))

        return min(score, 1.0)


class ProgressTracker:
    """Tracks useful work per step."""

    def __init__(self, config: Optional[dict] = None) -> None:
        self._total_questions_resolved = 0
        self._total_reflects = 0
        self._unique_files: set[str] = set()
        self._total_findings = 0
        self._steps = 0

    def update(
        self,
        sgr: Optional[SGREntry],
        tool_calls: list[dict],
        findings_count: int,
    ) -> float:
        """Record a step and return progress score (0.0 = no progress, 1.0 = great)."""
        self._steps += 1

        # Track unique files explored
        for tc in tool_calls:
            name = tc.get("name", "")
            if name in ("read_file", "read_outline"):
                path = tc.get("args", {}).get("path", "")
                if path:
                    self._unique_files.add(path)

        # Track SGR question resolution rate
        if sgr:
            self._total_reflects += 1
            self._total_questions_resolved += len(sgr.resolved_questions)

        # Track findings
        self._total_findings = findings_count

        # Compute composite score
        if self._steps == 0:
            return 0.5

        file_rate = min(len(self._unique_files) / max(self._steps, 1), 1.0)
        resolve_rate = (self._total_questions_resolved / max(self._total_reflects, 1)
                        if self._total_reflects > 0 else 0.5)

        return min((file_rate * 0.4 + resolve_rate * 0.4 + (0.2 if self._total_findings > 0 else 0)), 1.0)


class StuckDetector:
    """Combines repetition and progress to detect stuck state."""

    def __init__(
        self,
        window: int = 8,
        rep_threshold: float = 0.6,
        prog_threshold: float = 0.2,
        config: Optional[dict] = None,
    ) -> None:
        self.window = window
        self.rep_threshold = rep_threshold
        self.prog_threshold = prog_threshold
        self._config = config or {}
        self._actions = self._config.get("actions", [])

        if config:
            cond = config.get("condition", "")
            # Parse "repetition_score > 0.6 AND progress_score < 0.2"
            # Simple: override thresholds from config
            if ">" in cond:
                parts = cond.split("AND")
                for part in parts:
                    part = part.strip()
                    if "repetition_score" in part and ">" in part:
                        try:
                            self.rep_threshold = float(part.split(">")[1].strip())
                        except ValueError:
                            pass
                    if "progress_score" in part and "<" in part:
                        try:
                            self.prog_threshold = float(part.split("<")[1].strip())
                        except ValueError:
                            pass

    def check(self, rep_score: float, prog_score: float) -> bool:
        return rep_score > self.rep_threshold and prog_score < self.prog_threshold

    def get_actions(self) -> list[dict]:
        return list(self._actions)


class FeedbackEngine:
    """Combines all signal detectors, evaluated after each agent step."""

    def __init__(
        self,
        config: Optional[dict] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        config = config or {}
        self._event_bus = event_bus

        self.repetition = RepetitionDetector(
            window=config.get("repetition_score", {}).get("window", 6),
            config=config.get("repetition_score"),
        )
        self.progress = ProgressTracker(config=config.get("progress_score"))
        self.stuck_detector = StuckDetector(config=config.get("stuck_detector"))

        self._last_signals = FeedbackSignals()

    @property
    def signals(self) -> FeedbackSignals:
        return self._last_signals

    def after_step(
        self,
        step: int,
        tool_calls: list[dict],
        sgr: Optional[SGREntry] = None,
        findings_count: int = 0,
    ) -> FeedbackSignals:
        """Evaluate all signals after a step."""
        # Update repetition
        rep_score = 0.0
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            rep_score = max(rep_score, self.repetition.update(name, args))

        # Update progress
        prog_score = self.progress.update(sgr, tool_calls, findings_count)

        # Check stuck
        stuck = self.stuck_detector.check(rep_score, prog_score)

        signals = FeedbackSignals(
            repetition_score=rep_score,
            progress_score=prog_score,
            stuck=stuck,
        )
        self._last_signals = signals

        if stuck and self._event_bus:
            self._event_bus.emit(EventType.STUCK_DETECTED,
                                step=step,
                                repetition_score=rep_score,
                                progress_score=prog_score,
                                actions=self.stuck_detector.get_actions())

        return signals


def _args_hash(tool_name: str, args: dict) -> str:
    """Simple hash of tool name + sorted args for dedup detection."""
    parts = [tool_name]
    for k in sorted(args.keys()):
        v = args[k]
        parts.append(f"{k}={v}")
    return "|".join(parts)
