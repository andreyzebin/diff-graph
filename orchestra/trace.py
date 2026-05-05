"""
Trace data collection.

TraceCollector subscribes to EventBus and captures events.
collect_trace() builds the agent tree from in-memory agent data.
prepare_for_template() processes trace data for Jinja2 rendering.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import Agent
    from .sgr import SGREntry


# ── Event Collector ───────────────────────────────────────────────────────────

class TraceCollector:
    """Subscribes to EventBus and collects LLM request/response events per agent."""

    def __init__(self):
        self.llm_calls: dict[str, list[dict]] = defaultdict(list)

    def on_event(self, event_type: str, **kw):
        aid = kw.get("agent_id", "")
        if event_type == "agent_llm_request":
            messages = kw.get("messages", [])
            compact_msgs = []
            for m in messages:
                cm = {"role": m.get("role", "?")}
                content = m.get("content", "")
                if content:
                    cm["content"] = content
                if m.get("tool_calls"):
                    cm["tool_calls"] = [
                        {"name": tc.get("function", {}).get("name", "?"),
                         "args": tc.get("function", {}).get("arguments", "")[:300]}
                        for tc in m["tool_calls"]
                    ]
                compact_msgs.append(cm)

            self.llm_calls[aid].append({
                "step": kw.get("step", 0),
                "type": "request",
                "messages": compact_msgs,
                "message_count": len(messages),
                "llm_params": kw.get("llm_params", {}),
                "tools_count": len(kw.get("tools", [])),
            })
        elif event_type == "agent_llm_response":
            self.llm_calls[aid].append({
                "step": kw.get("step", 0),
                "type": "response",
                "tool_calls": kw.get("tool_calls", []),
                "content": kw.get("content", ""),
                "usage": kw.get("usage", {}),
            })


# ── Trace Collection ──────────────────────────────────────────────────────────

def collect_trace(agent: "Agent", collector: Optional[TraceCollector] = None) -> dict:
    """Recursively collect trace data from an agent tree."""
    results = agent._children_results if hasattr(agent, '_children_results') else {}
    children = agent._children if hasattr(agent, '_children') else {}

    sgr_history = agent.sgr.history if agent.sgr else []
    budget = agent.budget_state

    trace = {
        "agent_id": agent.agent_id,
        "agent_name": agent.config.name,
        "steps": budget.steps_used if budget else 0,
        "tokens_in": budget.tokens_in if budget else 0,
        "tokens_out": budget.tokens_out if budget else 0,
        "tokens_cached": budget.tokens_cached if budget else 0,
        "tokens_paid": budget.tokens_paid if budget else 0,
        "sgr": [_sgr_to_dict(e) for e in sgr_history],
        "llm_calls": [],
        "children": [],
        "output": None,
    }

    if collector:
        trace["llm_calls"] = collector.llm_calls.get(agent.agent_id, [])

    if agent._done_called:
        trace["output"] = agent._done_output

    for child_id, child_agent in children.items():
        child_trace = collect_trace(child_agent, collector)
        if child_id in results:
            child_trace["output"] = results[child_id].output
        trace["children"].append(child_trace)

    return trace


# ── Template Data Preparation ─────────────────────────────────────────────────

def prepare_for_template(trace: dict) -> dict:
    """Process trace into template-friendly structure with paired steps."""
    return _prepare_agent(trace, depth=0)


_SKIP_ARGS = {"spawn_agent", "reflect", "done"}
_SKIP_PARAMS = {"changes_only", "before", "after", "line_numbers", "ref", "regex"}


def _smart_truncate_path(path: str, max_len: int = 30) -> str:
    """Truncate path keeping the right (filename) part: …/model/Order.java."""
    if len(path) <= max_len or "/" not in path:
        return path
    parts = path.split("/")
    # Keep last 2 segments
    tail = "/".join(parts[-2:])
    if len(tail) + 2 <= max_len:
        return f"…/{tail}"
    # Just filename
    return f"…/{parts[-1]}"


def _tool_call_summary(tc: dict) -> dict:
    """Build summary with separate name and args for display."""
    name = tc.get("name", "?")
    if name in _SKIP_ARGS:
        return {"name": name, "args": ""}
    try:
        args = json.loads(tc.get("arguments", "{}") or "{}")
        # Filter out boilerplate params
        display_args = {k: v for k, v in args.items() if k not in _SKIP_PARAMS}
        vals = list(display_args.values())
        if not vals:
            return {"name": name, "args": ""}
        first = str(vals[0])
        # Smart truncate if it looks like a path
        if "/" in first:
            first = _smart_truncate_path(first)
        elif len(first) > 40:
            first = first[:40] + "…"
        return {"name": name, "args": first}
    except (json.JSONDecodeError, TypeError):
        return {"name": name, "args": ""}


def _prepare_agent(trace: dict, depth: int) -> dict:
    """Prepare one agent's data for template rendering."""
    sgr = trace.get("sgr", [])
    llm_calls = trace.get("llm_calls", [])
    sgr_by_step = {e["step"]: e for e in sgr}

    all_steps = sorted(set(
        [e["step"] for e in sgr] +
        [c["step"] for c in llm_calls]
    ))

    # Enrich tool calls with short argument summaries
    for call in llm_calls:
        if call["type"] == "response":
            for tc in call.get("tool_calls", []):
                tc["summary"] = _tool_call_summary(tc)

    # Build paired steps
    steps_list = []
    for step_num in all_steps:
        step_calls = [c for c in llm_calls if c["step"] == step_num]
        req = next((c for c in step_calls if c["type"] == "request"), None)
        resp = next((c for c in step_calls if c["type"] == "response"), None)
        steps_list.append({"step": step_num, "req": req, "resp": resp})

    # Compute per-step delta paid
    prev_paid = 0
    for sd in steps_list:
        resp = sd.get("resp")
        if resp:
            usage = resp.get("usage", {})
            current_paid = usage.get("paid", 0)
            usage["step_paid"] = current_paid - prev_paid if current_paid > prev_paid else current_paid
            prev_paid = current_paid

    # Pair tool results: NEW tool messages from next step's request
    prev_tool_count = 0
    paired_steps = []
    for i, sd in enumerate(steps_list):
        tool_results = []
        if i + 1 < len(steps_list) and steps_list[i + 1]["req"]:
            next_msgs = steps_list[i + 1]["req"].get("messages", [])
            all_tool_msgs = [m.get("content", "") for m in next_msgs if m.get("role") == "tool"]
            tool_results = all_tool_msgs[prev_tool_count:]
            prev_tool_count = len(all_tool_msgs)

        # Truncated tool results for display
        truncated_results = []
        for r in tool_results:
            truncated_results.append(r[:500] + ("…" if len(r) > 500 else ""))

        paired_steps.append({
            "step": sd["step"],
            "req": sd["req"],
            "resp": sd["resp"],
            "tool_results": truncated_results,
            "sgr": sgr_by_step.get(sd["step"]),
            "sgr_index": list(sgr_by_step.keys()).index(sd["step"]) if sd["step"] in sgr_by_step else None,
        })

    # Confidence trail
    conf_trail = [e.get("confidence", "?") for e in sgr]

    # Compute totals from usage
    total_in = 0
    total_out = 0
    total_cached = 0
    for sd in paired_steps:
        resp = sd.get("resp")
        if resp:
            usage = resp.get("usage", {})
            total_in += usage.get("prompt_tokens", 0) - usage.get("cached_tokens", 0)
            total_out += usage.get("completion_tokens", 0)
            total_cached += usage.get("cached_tokens", 0)

    return {
        "agent_id": trace.get("agent_id", ""),
        "agent_name": trace.get("agent_name", ""),
        "steps": trace.get("steps", 0),
        "tokens_paid": trace.get("tokens_paid", 0),
        "total_in": total_in,
        "total_out": total_out,
        "total_cached": total_cached,
        "conf_trail": conf_trail,
        "paired_steps": paired_steps,
        "children": [_prepare_agent(c, depth + 1) for c in trace.get("children", [])],
        "output": trace.get("output"),
        "depth": depth,
    }


def _sgr_to_dict(entry: "SGREntry") -> dict:
    return {
        "step": entry.step,
        "learned": entry.learned,
        "questions_remaining": entry.questions_remaining,
        "resolved_questions": entry.resolved_questions,
        "confidence": entry.confidence,
        "next_action": entry.next_action,
    }
