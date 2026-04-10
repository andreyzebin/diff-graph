"""
HTML trace renderer + event collector.

TraceCollector subscribes to EventBus and captures all events including
full LLM prompts and responses. After the run, collect_trace() merges
agent tree data with collected events for a complete picture.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import Agent
    from .sgr import SGREntry


# ── Event Collector ───────────────────────────────────────────────────────────

class TraceCollector:
    """Subscribes to EventBus and collects all events per agent."""

    def __init__(self):
        self.llm_calls: dict[str, list[dict]] = defaultdict(list)  # agent_id -> [{request, response}]

    def on_event(self, event_type: str, **kw):
        aid = kw.get("agent_id", "")
        if event_type == "agent_llm_request":
            # Serialize messages compactly
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
        "sgr": [_sgr_entry_to_dict(e) for e in sgr_history],
        "llm_calls": [],
        "children": [],
        "output": None,
    }

    # LLM calls from collector
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


# ── HTML Renderer ─────────────────────────────────────────────────────────────

def render_trace_body(trace: dict) -> tuple[str, str]:
    """Render the trace tree HTML + data blocks for template injection.
    Returns (trace_html, data_blocks_html)."""
    global _tab_counter
    _tab_counter = 0
    _data_blocks.clear()
    h = _H()
    _render_agent(h, trace, depth=0)
    data_h = _H()
    _flush_data_blocks(data_h)
    return h.build(), data_h.build()


_tab_counter = 0
_data_blocks: list[str] = []  # collected data blocks to emit at end

def _open_btn(title: str, content: str) -> str:
    """Generate [⧉] button + hidden data block. Content stored safely, not in onclick."""
    global _tab_counter
    _tab_counter += 1
    did = f"d{_tab_counter}"
    # Store content in a hidden script tag (type=text/data won't execute)
    # Only need to escape </script> inside
    safe_content = content.replace("</script>", "<\\/script>")
    _data_blocks.append(f'<script type="text/data" id="{did}">{safe_content}</script>')
    js_title = esc(title)
    return f'<span class="open-btn" onclick="openTabById(\'{js_title}\', \'{did}\')">⧉</span>'


def _download_btn(filename: str, content: str) -> str:
    """Generate [⬇] button that downloads content as JSON file."""
    global _tab_counter
    _tab_counter += 1
    did = f"dl{_tab_counter}"
    safe_content = content.replace("</script>", "<\\/script>")
    _data_blocks.append(f'<script type="text/data" id="{did}">{safe_content}</script>')
    return f'<span class="download-btn" onclick="event.stopPropagation(); event.preventDefault(); downloadJSON(\'{esc(filename)}\', \'{did}\')">📋 JSON</span>'


def _flush_data_blocks(h: _H):
    """Emit all stored data blocks."""
    for block in _data_blocks:
        h.line(block)
    _data_blocks.clear()


class _H:
    def __init__(self):
        self._lines: list[str] = []
    def line(self, s: str):
        self._lines.append(s)
    def build(self) -> str:
        return "\n".join(self._lines)


def esc(s: Any) -> str:
    return html.escape(str(s))


def _sgr_entry_to_dict(entry: "SGREntry") -> dict:
    return {
        "step": entry.step,
        "learned": entry.learned,
        "questions_remaining": entry.questions_remaining,
        "resolved_questions": entry.resolved_questions,
        "confidence": entry.confidence,
        "next_action": entry.next_action,
    }


def _conf_class(conf: str) -> str:
    return {"low": "conf-low", "medium": "conf-medium", "high": "conf-high"}.get(conf, "")


def _render_agent(h: _H, trace: dict, depth: int):
    name = esc(trace["agent_name"])
    steps = trace["steps"]
    paid = trace["tokens_paid"]
    agent_id = esc(trace["agent_id"])
    collapsed = "open" if depth == 0 else ""

    h.line(f'<details {collapsed} class="agent depth-{min(depth, 3)}">')
    h.line(f'<summary class="agent-header">')
    h.line(f'<span class="agent-name">{name}</span>')
    h.line(f'<span class="agent-meta">{steps} steps · paid {paid} tok · id:{agent_id}</span>')

    sgr = trace.get("sgr", [])
    if sgr:
        confs = [e["confidence"] for e in sgr]
        h.line('<span class="conf-trail">')
        for c in confs:
            h.line(f'<span class="conf-dot {_conf_class(c)}" title="{c}"></span>')
        h.line('</span>')
    h.line('</summary>')

    # Interleave SGR entries and LLM calls by step
    sgr_by_step = {e["step"]: e for e in sgr}
    llm_calls = trace.get("llm_calls", [])
    all_steps = sorted(set(
        [e["step"] for e in sgr] +
        [c["step"] for c in llm_calls]
    ))

    # Build steps with tool results paired
    steps_list = []
    for step_num in all_steps:
        step_calls = [c for c in llm_calls if c["step"] == step_num]
        req = next((c for c in step_calls if c["type"] == "request"), None)
        resp = next((c for c in step_calls if c["type"] == "response"), None)
        steps_list.append({"step": step_num, "req": req, "resp": resp})

    # Compute per-step delta paid (not cumulative)
    prev_paid = 0
    for sd in steps_list:
        resp = sd.get("resp")
        if resp:
            usage = resp.get("usage", {})
            current_paid = usage.get("paid", 0)
            usage["step_paid"] = current_paid - prev_paid if current_paid > prev_paid else current_paid
            prev_paid = current_paid

    # Pair tool results: get NEW tool messages from next step's request
    prev_tool_count = 0
    for i, sd in enumerate(steps_list):
        tool_results = []
        if i + 1 < len(steps_list) and steps_list[i + 1]["req"]:
            next_msgs = steps_list[i + 1]["req"].get("messages", [])
            all_tool_msgs = [m.get("content", "") for m in next_msgs if m.get("role") == "tool"]
            tool_results = all_tool_msgs[prev_tool_count:]
            prev_tool_count = len(all_tool_msgs)

        if sd["req"] or sd["resp"]:
            _render_llm_call(h, sd["step"], sd["req"], sd["resp"], tool_results)

        if sd["step"] in sgr_by_step:
            _render_sgr_entry(h, sgr_by_step[sd["step"]],
                              list(sgr_by_step.keys()).index(sd["step"]))

    # Children
    children = trace.get("children", [])
    if children:
        h.line('<div class="children-section">')
        h.line(f'<div class="spawn-label">spawned {len(children)} agent(s)</div>')
        for child in children:
            _render_agent(h, child, depth + 1)
        h.line('</div>')

    # Output / Findings
    output = trace.get("output")
    if output:
        _render_output(h, output)

    h.line('</details>')


def _render_llm_call(h: _H, step: int, req: Optional[dict], resp: Optional[dict],
                     tool_results: Optional[list[str]] = None):
    """Render a collapsible LLM call: context + call + result."""
    tool_results = tool_results or []

    # Summary line
    tool_names = ""
    usage_str = ""
    if resp:
        calls = resp.get("tool_calls", [])
        if calls:
            tool_names = ", ".join(c["name"] for c in calls[:3])
            if len(calls) > 3:
                tool_names += f" +{len(calls)-3}"
        usage = resp.get("usage", {})
        step_paid = usage.get("step_paid", usage.get("paid", 0))
        cached = usage.get("cached_tokens", 0)
        if step_paid:
            usage_str = f" · paid:{step_paid}"
            if cached:
                usage_str += f" (cache:{cached})"

    model = ""
    temp = ""
    if req:
        params = req.get("llm_params", {})
        model = params.get("model", "")
        temp = f" t={params.get('temperature', '?')}"

    h.line(f'<details class="llm-call">')
    h.line(f'<summary class="llm-header">')
    h.line(f'<span class="llm-step">step {step}</span>')
    if tool_names:
        h.line(f'<span class="llm-tools">{esc(tool_names)}</span>')
    if usage_str:
        h.line(f'<span class="llm-usage">{esc(usage_str)}</span>')
    if model:
        h.line(f'<span class="llm-model">{esc(model)}{esc(temp)}</span>')
    h.line('</summary>')

    # Call: what the LLM decided (tool calls with args)
    if resp:
        h.line(f'<div class="llm-section">')
        for tc in resp.get("tool_calls", []):
            tc_name = tc["name"]
            full_args = tc.get("arguments", "")
            btn = _open_btn(f"step {step} Call: {tc_name}", full_args)
            h.line(f'<div class="resp-tc">{esc(tc_name)}({esc(full_args[:500])}) {btn}</div>')
        usage = resp.get("usage", {})
        if usage:
            sp = usage.get("step_paid", usage.get("paid", 0))
            h.line(f'<div class="resp-usage">in:{usage.get("prompt_tokens",0)} out:{usage.get("completion_tokens",0)} cached:{usage.get("cached_tokens",0)} step_paid:{sp}</div>')
        h.line('</div>')

    # Result: what the tool returned
    if tool_results:
        h.line(f'<div class="llm-section">')
        h.line(f'<div class="llm-section-title">Result</div>')
        for ri, result in enumerate(tool_results):
            truncated = result[:1000]
            if len(result) > 1000:
                truncated += "…"
            h.line(f'<pre class="msg-content">{esc(truncated)}</pre>')
            h.line(_open_btn(f"step {step} Result", result))
        h.line('</div>')

    # Context: message history (collapsible, secondary)
    if req:
        msgs = req.get("messages", [])
        msgs_json = json.dumps(msgs, indent=2, default=str, ensure_ascii=False)
        dl_btn = _download_btn(f"step_{step}_messages.json", msgs_json)
        h.line(f'<details class="llm-context">')
        h.line(f'<summary class="llm-section-title">Context ({len(msgs)} messages) {dl_btn}</summary>')
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content", "")
            tcs = m.get("tool_calls", [])

            if content:
                preview = (content[:80] + "…") if len(content) > 80 else content
                preview = preview.replace("\n", " ")
            elif tcs:
                tc_names = ", ".join(
                    tc.get("name", "") or tc.get("function", {}).get("name", "?")
                    for tc in tcs[:3]
                )
                preview = f"→ {tc_names}" + (f" +{len(tcs)-3}" if len(tcs) > 3 else "")
            else:
                preview = "(empty)"

            h.line(f'<details class="msg-entry">')
            btn = _open_btn(f"{role}", content) if content else ""
            h.line(f'<summary class="msg-role msg-{role}">{role}: {esc(preview)} {btn}</summary>')
            if content:
                h.line(f'<pre class="msg-content">{esc(content)}</pre>')
            if tcs:
                for tc in tcs:
                    name = tc.get("name", "") or tc.get("function", {}).get("name", "?")
                    args = tc.get("args", "") or tc.get("function", {}).get("arguments", "")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    h.line(f'<div class="msg-tc">{esc(name)}({esc(str(args)[:300])})</div>')
            h.line('</details>')
        h.line('</details>')

    h.line('</details>')


def _render_sgr_entry(h: _H, entry: dict, index: int):
    conf = entry.get("confidence", "?")
    step = entry.get("step", "?")
    learned = entry.get("learned", "")
    questions = entry.get("questions_remaining", [])
    resolved = entry.get("resolved_questions", [])
    next_action = entry.get("next_action", "")

    h.line(f'<details class="sgr-entry {_conf_class(conf)}">')
    h.line(f'<summary class="sgr-header">')
    h.line(f'<span class="sgr-label">reflect #{index + 1}</span>')
    h.line(f'<span class="sgr-step">step {step}</span>')
    h.line(f'<span class="conf-badge {_conf_class(conf)}">{conf}</span>')
    if resolved:
        answered = sum(1 for r in resolved if isinstance(r, dict) and r.get("resolution") == "answered")
        dropped = len(resolved) - answered
        if answered:
            h.line(f'<span class="resolved-count">✓{answered}</span>')
        if dropped:
            h.line(f'<span class="dropped-count">✗{dropped}</span>')
    if questions:
        h.line(f'<span class="open-count">●{len(questions)}</span>')
    h.line('</summary>')

    if learned:
        h.line(f'<div class="sgr-learned"><strong>Learned:</strong> {esc(learned[:500])}</div>')

    if resolved:
        h.line('<div class="sgr-resolved">')
        for rq in resolved:
            if not isinstance(rq, dict):
                continue
            qid = esc(rq.get("id", rq.get("question", "?")))
            res = rq.get("resolution", "?")
            summary = esc(rq.get("summary", ""))
            icon = "✓" if res == "answered" else "✗"
            cls = "answered" if res == "answered" else "dropped"
            h.line(f'<div class="q-resolved {cls}"><span class="q-icon">{icon}</span> <span class="q-id">{qid}</span>')
            if summary:
                h.line(f'<span class="q-summary"> → {summary[:120]}</span>')
            h.line('</div>')
        h.line('</div>')

    if questions:
        h.line('<div class="sgr-open">')
        for q in questions:
            if isinstance(q, dict):
                qid = esc(q.get("id", "?"))
                text = esc(q.get("text", ""))
                h.line(f'<div class="q-open"><span class="q-dot">●</span> <span class="q-id">{qid}</span>: {text}</div>')
            else:
                h.line(f'<div class="q-open"><span class="q-dot">●</span> {esc(str(q))}</div>')
        h.line('</div>')

    if next_action:
        h.line(f'<div class="sgr-next"><strong>Next:</strong> {esc(next_action[:200])}</div>')
    h.line('</details>')


def _render_output(h: _H, output):
    h.line('<div class="output-section">')
    if isinstance(output, list):
        h.line(f'<div class="findings-header">{len(output)} finding(s)</div>')
        for f in output:
            if isinstance(f, dict):
                sev = esc(f.get("severity", "?"))
                title = esc(f.get("title", "?"))
                file = esc(f.get("file", ""))
                line = f.get("line", "")
                h.line(f'<div class="finding sev-{sev.lower()}">')
                h.line(f'<span class="sev-badge">{sev}</span> ')
                h.line(f'<strong>{title}</strong>')
                h.line(f'<span class="finding-loc">{file}:{line}</span>')
                h.line('</div>')
    else:
        h.line(f'<pre class="output-raw">{esc(json.dumps(output, indent=2, default=str)[:2000])}</pre>')
    h.line('</div>')


# ── CSS ───────────────────────────────────────────────────────────────────────

