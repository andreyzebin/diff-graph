"""
Adaptive LLM parameter resolution.

Computes per-step LLM params (temperature, top_p, penalties, model)
from static defaults + schedule overrides driven by agent state.
"""
from __future__ import annotations

import importlib
import logging
import math
from typing import Any, Callable, Optional, Protocol

from .types import LLMParamsConfig, ModelScheduleEntry, ScheduleConfig

log = logging.getLogger(__name__)


# ── Schedule drivers (signal sources) ─────────────────────────────────────────

class ScheduleDriver(Protocol):
    def value(self, state: dict) -> float: ...


class BudgetRatioDriver:
    def value(self, state: dict) -> float:
        return state.get("budget_ratio", 0.0)


class StepRatioDriver:
    def value(self, state: dict) -> float:
        return state.get("step_ratio", 0.0)


class SGRConfidenceDriver:
    def value(self, state: dict) -> float:
        return state.get("sgr_confidence", 0.0)


class SGRQuestionCountDriver:
    def value(self, state: dict) -> float:
        count = state.get("sgr_question_count", 0)
        # Normalize: 0 questions = 0.0, 5+ questions = 1.0
        return min(count / 5.0, 1.0)


class SGRStalenessDriver:
    def value(self, state: dict) -> float:
        staleness = state.get("sgr_staleness", 0)
        return min(staleness / 5.0, 1.0)


class RepetitionScoreDriver:
    def value(self, state: dict) -> float:
        return state.get("repetition_score", 0.0)


class CustomDriver:
    def __init__(self, handler: Callable) -> None:
        self._handler = handler

    def value(self, state: dict) -> float:
        try:
            return float(self._handler(state))
        except Exception:
            return 0.0


_DRIVERS: dict[str, type] = {
    "budget_ratio": BudgetRatioDriver,
    "step_ratio": StepRatioDriver,
    "sgr_confidence": SGRConfidenceDriver,
    "sgr_question_count": SGRQuestionCountDriver,
    "sgr_staleness": SGRStalenessDriver,
    "repetition_score": RepetitionScoreDriver,
}


def get_driver(name: str, handler: Optional[str] = None) -> ScheduleDriver:
    if name == "custom" and handler:
        fn = _import_callable(handler)
        return CustomDriver(fn) if fn else BudgetRatioDriver()
    cls = _DRIVERS.get(name)
    if cls:
        return cls()
    log.warning("unknown driver '%s', falling back to budget_ratio", name)
    return BudgetRatioDriver()


# ── Curve functions ───────────────────────────────────────────────────────────

class CurveFunction(Protocol):
    def apply(self, driver_value: float, range_vals: list[float]) -> float: ...


class LinearCurve:
    def apply(self, v: float, r: list[float]) -> float:
        return r[0] + (r[1] - r[0]) * v


class LinearDecayCurve:
    def apply(self, v: float, r: list[float]) -> float:
        return r[0] + (r[1] - r[0]) * v


class InverseLinearCurve:
    def apply(self, v: float, r: list[float]) -> float:
        return r[0] + (r[1] - r[0]) * (1.0 - v)


class ExponentialDecayCurve:
    def apply(self, v: float, r: list[float]) -> float:
        # Fast decay early, slow later
        decay = math.exp(-3 * v)
        return r[1] + (r[0] - r[1]) * decay


class StepCurve:
    def __init__(self, steps: dict[str, float]) -> None:
        # steps: {"0.0": value, "0.5": value, "0.8": value}
        self._steps = sorted(
            [(float(k), v) for k, v in steps.items()],
            key=lambda x: x[0],
        )

    def apply(self, v: float, r: list[float]) -> float:
        result = r[0] if r else 0.0
        for threshold, value in self._steps:
            if v >= threshold:
                result = value
        return result


class CustomCurve:
    def __init__(self, handler: Callable) -> None:
        self._handler = handler

    def apply(self, v: float, r: list[float]) -> float:
        try:
            return float(self._handler(v, r))
        except Exception:
            return r[0] if r else 0.0


_CURVES: dict[str, type] = {
    "linear": LinearCurve,
    "linear_decay": LinearDecayCurve,
    "inverse_linear": InverseLinearCurve,
    "exponential_decay": ExponentialDecayCurve,
}


def get_curve(name: str, steps: Optional[dict] = None,
              handler: Optional[str] = None) -> CurveFunction:
    if name == "step" and steps:
        return StepCurve(steps)
    if name == "custom" and handler:
        fn = _import_callable(handler)
        return CustomCurve(fn) if fn else LinearCurve()
    cls = _CURVES.get(name)
    if cls:
        return cls()
    log.warning("unknown curve '%s', falling back to linear", name)
    return LinearCurve()


# ── Adaptive resolver ─────────────────────────────────────────────────────────

class AdaptiveParamResolver:
    """Resolves LLM params for a given step based on schedules + agent state."""

    def __init__(self, config: Optional[LLMParamsConfig] = None) -> None:
        self.config = config
        self._schedule_entries: list[_ScheduleEntry] = []

        if config:
            for sc in config.schedules:
                driver = get_driver(sc.driver, handler=sc.handler)
                curve = get_curve(sc.curve, steps=sc.steps, handler=sc.handler)
                self._schedule_entries.append(_ScheduleEntry(
                    param=sc.param,
                    driver=driver,
                    curve=curve,
                    range=sc.range or [0.0, 1.0],
                    merge_policy=sc.merge_policy,
                ))

    def resolve(self, agent_state: dict) -> dict[str, Any]:
        """Returns effective {temperature, top_p, ...} for this step."""
        if not self.config:
            return {}

        # Start with static defaults
        params: dict[str, Any] = {
            "temperature": self.config.temperature,
        }
        if self.config.top_p != 1.0:
            params["top_p"] = self.config.top_p
        if self.config.frequency_penalty:
            params["frequency_penalty"] = self.config.frequency_penalty
        if self.config.presence_penalty:
            params["presence_penalty"] = self.config.presence_penalty
        if self.config.max_completion_tokens:
            params["max_completion_tokens"] = self.config.max_completion_tokens

        # Group schedule entries by param
        param_values: dict[str, list[tuple[float, str]]] = {}

        for entry in self._schedule_entries:
            driver_val = entry.driver.value(agent_state)
            computed = entry.curve.apply(driver_val, entry.range)
            param_values.setdefault(entry.param, []).append((computed, entry.merge_policy))

        # Merge and apply overrides
        for param_name, values in param_values.items():
            merged = self._merge(values)
            params[param_name] = merged

        return params

    def resolve_model(self, agent_state: dict) -> Optional[str]:
        """Returns effective model for this step, or None for default."""
        if not self.config or not self.config.model_schedule:
            return self.config.model if self.config else None

        phase = agent_state.get("phase", "tool_calls")
        for entry in self.config.model_schedule:
            if entry.phase == phase:
                if entry.condition:
                    if _eval_condition(entry.condition, agent_state):
                        return entry.model
                else:
                    return entry.model
        return self.config.model if self.config else None

    @staticmethod
    def _merge(values: list[tuple[float, str]]) -> float:
        """Merge multiple schedule values for the same param."""
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0][0]

        # Use the merge policy of the last entry
        policy = values[-1][1]
        nums = [v for v, _ in values]

        if policy == "min":
            return min(nums)
        elif policy == "max":
            return max(nums)
        elif policy == "mean":
            return sum(nums) / len(nums)
        else:  # last_wins
            return nums[-1]


class ModelSwitcher:
    """Selects model based on phase and conditions."""

    def __init__(self, schedule: list[ModelScheduleEntry]) -> None:
        self._schedule = schedule

    def select(self, phase: str, agent_state: dict) -> Optional[str]:
        for entry in self._schedule:
            if entry.phase == phase:
                if entry.condition:
                    if _eval_condition(entry.condition, agent_state):
                        return entry.model
                else:
                    return entry.model
        return None


# ── Internal ──────────────────────────────────────────────────────────────────

class _ScheduleEntry:
    def __init__(self, param: str, driver: ScheduleDriver, curve: CurveFunction,
                 range: list[float], merge_policy: str) -> None:
        self.param = param
        self.driver = driver
        self.curve = curve
        self.range = range
        self.merge_policy = merge_policy


def _eval_condition(condition: str, state: dict) -> bool:
    """Simple condition evaluator: 'key op value'."""
    try:
        # Support: "stuck == true", "tool_count == 1", "sgr_confidence < 0.5"
        for op in ("==", "!=", ">=", "<=", ">", "<"):
            if op in condition:
                left, right = condition.split(op, 1)
                left = left.strip()
                right = right.strip()
                left_val = state.get(left, left)
                # Try numeric comparison
                try:
                    left_num = float(left_val)
                    right_num = float(right)
                    if op == "==": return left_num == right_num
                    if op == "!=": return left_num != right_num
                    if op == ">=": return left_num >= right_num
                    if op == "<=": return left_num <= right_num
                    if op == ">": return left_num > right_num
                    if op == "<": return left_num < right_num
                except (ValueError, TypeError):
                    pass
                # String comparison
                if op == "==": return str(left_val) == right
                if op == "!=": return str(left_val) != right
                break
    except Exception:
        pass
    return False


def _import_callable(dotted_path: str) -> Optional[Callable]:
    try:
        mod_path, _, attr = dotted_path.rpartition(".")
        if mod_path:
            mod = importlib.import_module(mod_path)
            return getattr(mod, attr)
    except Exception:
        log.warning("could not import: %s", dotted_path)
    return None
