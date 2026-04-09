"""
HTML trace renderer.

Collects the full agent execution tree and renders it as a
collapsible HTML document for post-run analysis.
"""
from __future__ import annotations

import html
import json
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import Agent, AgentResult
    from .sgr import SGREntry


def collect_trace(agent: "Agent") -> dict:
    """Recursively collect trace data from an agent and its children."""
    result = agent._children_results if hasattr(agent, '_children_results') else {}
    children_agents = agent._children if hasattr(agent, '_children') else {}

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
        "actions": [],
        "children": [],
        "output": None,
    }

    # Collect actions from trace steps
    if hasattr(agent, 'budget_state') and agent.budget_state:
        for sr in (agent.budget_state.fired_pushers if hasattr(agent.budget_state, 'fired_pushers') else []):
            pass  # pushers not useful for trace

    # Get output from children results or agent's own done
    if agent._done_called:
        trace["output"] = agent._done_output

    # Collect children traces recursively
    for child_id, child_agent in children_agents.items():
        child_trace = collect_trace(child_agent)
        # Add child's output from results if available
        if child_id in result:
            child_result = result[child_id]
            child_trace["output"] = child_result.output
        trace["children"].append(child_trace)

    return trace


def render_html(trace: dict, title: str = "Review Trace") -> str:
    """Render a trace tree as a self-contained HTML document."""
    h = _H()
    h.line("<!DOCTYPE html>")
    h.line("<html><head>")
    h.line(f"<title>{esc(title)}</title>")
    h.line("<meta charset='utf-8'>")
    h.line(f"<style>{_CSS}</style>")
    h.line("</head><body>")
    h.line(f"<h1>{esc(title)}</h1>")
    _render_agent(h, trace, depth=0)
    h.line(f"<script>{_JS}</script>")
    h.line("</body></html>")
    return h.build()


# ── Internal ──────────────────────────────────────────────────────────────────

class _H:
    """Simple HTML builder."""
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

    # Confidence badges
    sgr = trace.get("sgr", [])
    if sgr:
        confs = [e["confidence"] for e in sgr]
        h.line('<span class="conf-trail">')
        for c in confs:
            h.line(f'<span class="conf-dot {_conf_class(c)}" title="{c}"></span>')
        h.line('</span>')
    h.line('</summary>')

    # SGR timeline
    for i, entry in enumerate(sgr):
        _render_sgr_entry(h, entry, i)

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

    # Learned
    if learned:
        h.line(f'<div class="sgr-learned"><strong>Learned:</strong> {esc(learned[:500])}</div>')

    # Resolved
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

    # Open
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

    # Next action
    if next_action:
        h.line(f'<div class="sgr-next"><strong>Next:</strong> {esc(next_action[:200])}</div>')

    h.line('</details>')


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

.conf-badge { padding: 1px 8px; border-radius: 10px; font-size: 0.8em; font-weight: bold;
              border: 1px solid; }
.conf-badge.conf-low { border-color: #da363366; color: #f47067; background: none; }
.conf-badge.conf-medium { border-color: #9e6a0366; color: #c69026; background: none; }
.conf-badge.conf-high { border-color: #23863666; color: #56d364; background: none; }

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
.sev-badge { padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold;
             border: 1px solid; background: none; }
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
"""
