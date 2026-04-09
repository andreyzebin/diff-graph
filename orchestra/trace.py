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
                    cm["content"] = content[:2000] + ("…" if len(content) > 2000 else "")
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

def render_html(trace: dict, title: str = "Review Trace") -> str:
    h = _H()
    h.line("<!DOCTYPE html><html><head>")
    h.line(f"<title>{esc(title)}</title>")
    h.line("<meta charset='utf-8'>")
    h.line(f"<style>{_CSS}</style>")
    h.line("</head><body>")
    h.line(f"<h1>{esc(title)}</h1>")
    _render_agent(h, trace, depth=0)
    h.line(f"<script>{_JS}</script>")
    h.line("</body></html>")
    return h.build()


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

    for step_num in all_steps:
        # LLM request/response for this step
        step_calls = [c for c in llm_calls if c["step"] == step_num]
        req = next((c for c in step_calls if c["type"] == "request"), None)
        resp = next((c for c in step_calls if c["type"] == "response"), None)

        if req or resp:
            _render_llm_call(h, step_num, req, resp)

        # SGR entry for this step
        if step_num in sgr_by_step:
            _render_sgr_entry(h, sgr_by_step[step_num],
                              list(sgr_by_step.keys()).index(step_num))

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


def _render_llm_call(h: _H, step: int, req: Optional[dict], resp: Optional[dict]):
    """Render a collapsible LLM request/response pair."""
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
        paid = usage.get("paid", 0)
        cached = usage.get("cached_tokens", 0)
        if paid:
            usage_str = f" · paid:{paid}"
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

    # Request details
    if req:
        msgs = req.get("messages", [])
        h.line(f'<div class="llm-section">')
        h.line(f'<div class="llm-section-title">Request ({len(msgs)} messages, {req.get("tools_count", 0)} tools)</div>')
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content", "")
            h.line(f'<details class="msg-entry">')
            preview = (content[:80] + "…") if len(content) > 80 else content
            preview = preview.replace("\n", " ")
            h.line(f'<summary class="msg-role msg-{role}">{role}: {esc(preview)}</summary>')
            if content:
                h.line(f'<pre class="msg-content">{esc(content)}</pre>')
            tcs = m.get("tool_calls", [])
            if tcs:
                for tc in tcs:
                    h.line(f'<div class="msg-tc">{esc(tc.get("name", "?"))}({esc(tc.get("args", "")[:200])})</div>')
            h.line('</details>')
        h.line('</div>')

    # Response details
    if resp:
        h.line(f'<div class="llm-section">')
        h.line(f'<div class="llm-section-title">Response</div>')
        for tc in resp.get("tool_calls", []):
            h.line(f'<div class="resp-tc">{esc(tc["name"])}({esc(tc.get("arguments", "")[:300])})</div>')
        content = resp.get("content", "")
        if content:
            h.line(f'<pre class="msg-content">{esc(content)}</pre>')
        usage = resp.get("usage", {})
        if usage:
            h.line(f'<div class="resp-usage">in:{usage.get("prompt_tokens",0)} out:{usage.get("completion_tokens",0)} cached:{usage.get("cached_tokens",0)} paid:{usage.get("paid",0)}</div>')
        h.line('</div>')

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

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1200px; margin: 0 auto; }
h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }

details { margin: 4px 0; }
summary { cursor: pointer; user-select: none; }
summary:hover { background: #161b22; }

.agent { border: 1px solid #30363d; border-radius: 6px; margin: 8px 0; background: #0d1117; }
.agent-header { padding: 8px 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.agent-name { font-weight: bold; color: #58a6ff; font-size: 1.1em; }
.agent-meta { color: #8b949e; font-size: 0.85em; }
.depth-1 { margin-left: 16px; border-color: #1f6feb33; }
.depth-2 { margin-left: 32px; border-color: #1f6feb22; }
.depth-3 { margin-left: 48px; border-color: #1f6feb11; }

.conf-trail { display: flex; gap: 3px; align-items: center; }
.conf-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.conf-low { background: #da3633; }
.conf-medium { background: #9e6a03; }
.conf-high { background: #238636; }

.conf-badge { padding: 1px 8px; border-radius: 10px; font-size: 0.8em; font-weight: bold; border: 1px solid; }
.conf-badge.conf-low { border-color: #da363366; color: #f47067; background: none; }
.conf-badge.conf-medium { border-color: #9e6a0366; color: #c69026; background: none; }
.conf-badge.conf-high { border-color: #23863666; color: #56d364; background: none; }

/* LLM calls */
.llm-call { margin: 2px 12px; border-left: 2px solid #30363d; }
.llm-header { padding: 4px 10px; display: flex; gap: 8px; align-items: center; font-size: 0.85em; }
.llm-step { color: #8b949e; }
.llm-tools { color: #79c0ff; }
.llm-usage { color: #8b949e; }
.llm-model { color: #8b949e; font-size: 0.8em; }
.llm-section { padding: 4px 12px; }
.llm-section-title { color: #8b949e; font-size: 0.8em; font-weight: bold; margin: 4px 0 2px; }
.msg-entry { margin: 1px 0; }
.msg-role { font-size: 0.8em; padding: 2px 8px; }
.msg-system { color: #c69026; }
.msg-user { color: #56d364; }
.msg-assistant { color: #79c0ff; }
.msg-tool { color: #8b949e; }
.msg-content { padding: 4px 12px; background: #161b22; border-radius: 4px; font-size: 0.75em;
               overflow-x: auto; white-space: pre-wrap; max-height: 300px; overflow-y: auto;
               margin: 2px 0; color: #8b949e; }
.msg-tc { padding: 2px 12px; font-size: 0.8em; color: #79c0ff; }
.resp-tc { padding: 2px 0; font-size: 0.85em; color: #79c0ff; }
.resp-usage { font-size: 0.8em; color: #8b949e; padding: 2px 0; }

/* SGR entries */
.sgr-entry { margin: 2px 12px; border-left: 2px solid #30363d; }
.sgr-entry.conf-low { border-left-color: #da3633; }
.sgr-entry.conf-medium { border-left-color: #9e6a03; }
.sgr-entry.conf-high { border-left-color: #238636; }
.sgr-header { padding: 4px 10px; display: flex; gap: 8px; align-items: center; font-size: 0.9em; }
.sgr-label { color: #8b949e; }
.sgr-step { color: #8b949e; font-size: 0.85em; }
.resolved-count { color: #56d364; font-size: 0.85em; }
.dropped-count { color: #8b949e; font-size: 0.85em; }
.open-count { color: #c69026; font-size: 0.85em; }
.sgr-learned, .sgr-next { padding: 4px 12px; color: #8b949e; font-size: 0.85em; }
.sgr-resolved, .sgr-open { padding: 2px 12px; }
.q-resolved { padding: 2px 0; font-size: 0.85em; }
.q-resolved.answered { color: #56d364; }
.q-resolved.dropped { color: #8b949e; }
.q-icon { font-weight: bold; }
.q-id { font-weight: bold; color: #79c0ff; }
.q-summary { color: #8b949e; font-style: italic; }
.q-open { padding: 2px 0; font-size: 0.85em; color: #c69026; }
.q-dot { color: #c69026; }

.children-section { padding: 4px 0; }
.spawn-label { padding: 4px 12px; color: #8b949e; font-size: 0.85em; font-style: italic; }

.output-section { padding: 8px 12px; border-top: 1px solid #30363d; }
.findings-header { color: #79c0ff; font-weight: bold; margin-bottom: 4px; }
.finding { padding: 3px 0; font-size: 0.9em; display: flex; gap: 8px; align-items: baseline; }
.sev-badge { padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; border: 1px solid; background: none; }
.sev-blocker { border-color: #da363366; color: #f47067; }
.sev-major { border-color: #9e6a0366; color: #c69026; }
.sev-minor { border-color: #388bfd33; color: #79c0ff; }
.sev-comment { border-color: #8b949e33; color: #8b949e; }
.finding-loc { color: #8b949e; font-size: 0.8em; }
.output-raw { padding: 8px; background: #161b22; border-radius: 4px; font-size: 0.8em;
              overflow-x: auto; white-space: pre-wrap; }
"""

_JS = """
document.querySelectorAll('.output-section').forEach(el => {
  let details = el.closest('details');
  if (details) details.open = true;
});
"""
