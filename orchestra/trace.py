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
    h.line('<div class="layout">')
    h.line('<div class="left-pane">')
    h.line(f"<h1>{esc(title)}</h1>")
    _render_agent(h, trace, depth=0)
    h.line('</div>')
    h.line('<div class="right-pane" id="detail-panel">')
    h.line('<div class="tab-bar" id="tab-bar"><span class="tab-hint">Click [⧉] to open details</span></div>')
    h.line('<div class="tab-content" id="tab-content"></div>')
    h.line('</div>')
    h.line('</div>')
    h.line(f"<script>{_JS}</script>")
    h.line("</body></html>")
    return h.build()


_tab_counter = 0

def _open_btn(title: str, content: str) -> str:
    """Generate an inline [⧉] button that opens content in the right panel."""
    global _tab_counter
    _tab_counter += 1
    tid = f"tab_{_tab_counter}"
    # Escape for JS string (single quotes, newlines, backslashes)
    js_title = title.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    js_content = content.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("</", "<\\/")
    return f'<span class="open-btn" onclick="openTab(\'{js_title}\', \'{js_content}\', \'{tid}\')">⧉</span>'


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

    # Call: what the LLM decided (tool calls with args)
    if resp:
        h.line(f'<div class="llm-section">')
        call_parts = []
        for tc in resp.get("tool_calls", []):
            full_args = tc.get("arguments", "")
            call_parts.append(f'{tc["name"]}({full_args})')
            h.line(f'<div class="resp-tc">{esc(tc["name"])}({esc(full_args[:500])}) {_open_btn(f"step {step} Call: {tc['name']}", full_args)}</div>')
        usage = resp.get("usage", {})
        if usage:
            h.line(f'<div class="resp-usage">in:{usage.get("prompt_tokens",0)} out:{usage.get("completion_tokens",0)} cached:{usage.get("cached_tokens",0)} paid:{usage.get("paid",0)}</div>')
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
        h.line(f'<details class="llm-context">')
        h.line(f'<summary class="llm-section-title">Context ({len(msgs)} messages)</summary>')
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

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; padding: 0; }
h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }

/* Split-pane layout */
.layout { display: flex; height: 100vh; }
.left-pane { flex: 1; overflow-y: auto; padding: 20px; min-width: 400px; }
.right-pane { width: 50%; min-width: 300px; border-left: 1px solid #30363d;
              display: flex; flex-direction: column; background: #0d1117; }
.tab-bar { display: flex; gap: 0; overflow-x: auto; background: #161b22;
           border-bottom: 1px solid #30363d; min-height: 32px; align-items: center; flex-shrink: 0; }
.tab-hint { color: #8b949e; font-size: 0.8em; padding: 0 12px; }
.tab-btn { padding: 6px 12px; font-size: 0.8em; cursor: pointer; border: none;
           background: none; color: #8b949e; border-bottom: 2px solid transparent;
           white-space: nowrap; display: flex; align-items: center; gap: 6px; }
.tab-btn:hover { color: #c9d1d9; background: #1c2129; }
.tab-btn.active { color: #58a6ff; border-bottom-color: #58a6ff; background: #0d1117; }
.tab-close { font-size: 1em; opacity: 0.5; }
.tab-close:hover { opacity: 1; color: #f47067; }
.tab-content { flex: 1; overflow: auto; padding: 12px; }
.tab-content pre { white-space: pre-wrap; word-break: break-all; font-size: 0.8em;
                   color: #c9d1d9; line-height: 1.5; }

/* Open-in-panel button */
.open-btn { cursor: pointer; color: #8b949e; font-size: 0.75em; margin-left: 4px;
            opacity: 0.5; }
.open-btn:hover { opacity: 1; color: #58a6ff; }

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
.llm-context { margin: 2px 0; }
.llm-context > summary { color: #8b949e; font-size: 0.8em; cursor: pointer; padding: 2px 12px; }
.llm-context > summary:hover { color: #c9d1d9; }

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
// Auto-expand agents with findings
document.querySelectorAll('.output-section').forEach(el => {
  let details = el.closest('details');
  if (details) details.open = true;
});

// Tab system for right panel
const tabs = {};
let activeTab = null;

function openTab(title, content, id) {
  const panel = document.getElementById('detail-panel');
  const bar = document.getElementById('tab-bar');
  const area = document.getElementById('tab-content');

  // Remove hint
  const hint = bar.querySelector('.tab-hint');
  if (hint) hint.remove();

  // If tab already exists, just activate it
  if (tabs[id]) {
    activateTab(id);
    return;
  }

  // Create tab button
  const btn = document.createElement('div');
  btn.className = 'tab-btn';
  btn.dataset.id = id;
  btn.innerHTML = '<span class="tab-title">' + escHtml(title.substring(0, 30)) + '</span>' +
                  '<span class="tab-close" onclick="event.stopPropagation(); closeTab(\\'' + id + '\\')">×</span>';
  btn.onclick = () => activateTab(id);
  bar.appendChild(btn);

  // Store tab data
  tabs[id] = { title, content, btn };

  // Activate
  activateTab(id);
}

function activateTab(id) {
  const area = document.getElementById('tab-content');
  const tab = tabs[id];
  if (!tab) return;

  // Deactivate all
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

  // Activate this one
  tab.btn.classList.add('active');
  activeTab = id;

  // Try to format as JSON, otherwise plain text
  let display = tab.content;
  try {
    const parsed = JSON.parse(display);
    display = JSON.stringify(parsed, null, 2);
  } catch(e) {}

  area.innerHTML = '<pre>' + escHtml(display) + '</pre>';
}

function closeTab(id) {
  const tab = tabs[id];
  if (!tab) return;
  tab.btn.remove();
  delete tabs[id];

  if (activeTab === id) {
    const remaining = Object.keys(tabs);
    if (remaining.length > 0) {
      activateTab(remaining[remaining.length - 1]);
    } else {
      document.getElementById('tab-content').innerHTML = '';
      const bar = document.getElementById('tab-bar');
      bar.innerHTML = '<span class="tab-hint">Click [⧉] to open details</span>';
    }
  }
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
"""
