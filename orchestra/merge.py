"""
Fork/join merge strategies.

When multiple agent branches complete (from fork or fan_out),
a merge strategy combines their results into a single output.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import AgentResult

log = logging.getLogger(__name__)


class MergeStrategy(Protocol):
    def merge(self, results: list["AgentResult"]) -> Any: ...


class BestConfidenceMerge:
    """Take results from the branch with highest SGR confidence."""

    def merge(self, results: list["AgentResult"]) -> Any:
        if not results:
            return None

        best = results[0]
        best_conf = 0.0

        for r in results:
            if r.sgr_history:
                conf_map = {"low": 0.0, "medium": 0.5, "high": 1.0}
                conf = conf_map.get(r.sgr_history[-1].confidence, 0.0)
                if conf > best_conf:
                    best_conf = conf
                    best = r

        return best.output


class UnionMerge:
    """Concatenate all outputs. If outputs are lists, flatten and deduplicate by title."""

    def merge(self, results: list["AgentResult"]) -> Any:
        all_items: list = []
        seen_titles: set = set()

        for r in results:
            output = r.output
            if isinstance(output, list):
                for item in output:
                    title = item.get("title", "") if isinstance(item, dict) else str(item)
                    if title not in seen_titles:
                        seen_titles.add(title)
                        all_items.append(item)
            elif isinstance(output, dict):
                # If dict has a list field (e.g., "findings"), extract it
                for v in output.values():
                    if isinstance(v, list):
                        for item in v:
                            title = item.get("title", "") if isinstance(item, dict) else str(item)
                            if title not in seen_titles:
                                seen_titles.add(title)
                                all_items.append(item)
                        break
                else:
                    all_items.append(output)
            elif output is not None:
                all_items.append(output)

        return all_items


class LLMMerge:
    """Run an LLM call to reconcile results from multiple branches."""

    def __init__(self, llm: Any = None, model: str = "") -> None:
        self._llm = llm
        self._model = model

    def merge(self, results: list["AgentResult"]) -> Any:
        if not results:
            return None
        if not self._llm:
            return UnionMerge().merge(results)

        # Build prompt with all branch outputs
        parts = []
        for i, r in enumerate(results):
            output_str = json.dumps(r.output, indent=2, ensure_ascii=False) if r.output else "(no output)"
            confidence = r.sgr_history[-1].confidence if r.sgr_history else "unknown"
            parts.append(f"Branch {i+1} (confidence: {confidence}):\n{output_str}")

        prompt = (
            "Multiple agent branches investigated the same codebase in parallel. "
            "Merge their findings: deduplicate, resolve conflicts (prefer higher confidence), "
            "and produce a single consolidated list.\n\n"
            + "\n\n---\n\n".join(parts)
        )

        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": "You merge code review findings. Output JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                stream=False,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(content)
        except Exception as e:
            log.warning("LLM merge failed: %s, falling back to union", e)
            return UnionMerge().merge(results)


class CustomMerge:
    """User-provided Python callable."""

    def __init__(self, handler: Callable) -> None:
        self._handler = handler

    def merge(self, results: list["AgentResult"]) -> Any:
        return self._handler(results)


# ── Factory ───────────────────────────────────────────────────────────────────

def get_merge_strategy(
    name: str,
    llm: Any = None,
    model: str = "",
    handler: Optional[Callable] = None,
) -> MergeStrategy:
    if name == "best_confidence":
        return BestConfidenceMerge()
    elif name == "union":
        return UnionMerge()
    elif name == "llm_merge":
        return LLMMerge(llm=llm, model=model)
    elif name == "custom" and handler:
        return CustomMerge(handler)
    else:
        log.warning("unknown merge strategy '%s', falling back to union", name)
        return UnionMerge()
