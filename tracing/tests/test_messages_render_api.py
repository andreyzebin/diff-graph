"""API plumbing for the server-side message renderer.

Pins the contract that the QA UI's history tab and the
`quality-cli traces messages` command both rely on:

  - `/api/runs/{id}/step/{agent}/{n}/messages` returns the raw JSON
    array by default (back-compat with existing clients).
  - The same endpoint with `?as=text` returns plain text rendered
    by `tracing.server.messages_render.render_messages` —
    PlainTextResponse, not JSON-wrapped.
  - `/api/runs/{id}/step/{agent}/{n}/call` follows the same
    convention with `render_call`.
  - 404s use PlainTextResponse for the text variant so the CLI
    can pipe the output without unwrapping a JSON error envelope.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def db_with_step(monkeypatch):
    """Drop a temp SQLite that the app's per-step endpoints will read.

    The two endpoints under test hit `DEFAULT_DB_PATH` directly via
    `sqlite3.connect(...)`, not through the store abstraction, so
    monkeypatching the constant in `tracing.server.app` is enough.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    con = sqlite3.connect(str(db_path))
    con.execute("""
      CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        agent_id TEXT,
        event_type TEXT,
        step INTEGER,
        data_json TEXT
      )
    """)
    # Step 0 request: system + user prompt the agent saw before the
    # first tool call. The renderer should label them with role banners.
    messages_step0 = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user", "content": "Please review the diff."},
    ]
    # Tools the agent had at step 0 — full OpenAI shape with name,
    # description, and parameter schema. The /tools endpoint serves
    # this back as-is; the renderer formats it human-readably.
    tools_step0 = [
        {"type": "function", "function": {
            "name": "diff_read_file",
            "description": "Read a slice of a file in the diff view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        }},
        {"type": "function", "function": {
            "name": "agent_spawn",
            "description": "Delegate a focused investigation to a sub-agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "focus": {"type": "string"},
                },
                "required": ["agent_name", "focus"],
            },
        }},
    ]
    con.execute(
        "INSERT INTO events (run_id, agent_id, event_type, step, data_json) "
        "VALUES (?, ?, 'agent_llm_request', 0, ?)",
        ("run-x", "agent-x", json.dumps({
            "messages": messages_step0,
            "tools": tools_step0,
            "tools_count": len(tools_step0),
        })),
    )
    # Step 1 request: legacy-shape event — only `tools_count`, no
    # full schemas (orchestra/trace_db.py used to drop the array).
    # The /tools endpoint must degrade gracefully — empty list +
    # the count, with a hint string in the text view.
    con.execute(
        "INSERT INTO events (run_id, agent_id, event_type, step, data_json) "
        "VALUES (?, ?, 'agent_llm_request', 1, ?)",
        ("run-x", "agent-x", json.dumps({
            "messages": messages_step0,
            "tools_count": 7,
        })),
    )
    # Step 0 response: tool call (well-formed) — exercises the
    # /call endpoint's pretty-printed args branch.
    response_step0 = {
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "function": {
                "name": "diff_read_file",
                "arguments": '{"path":"src/Foo.java","start_line":40}',
            },
        }],
    }
    con.execute(
        "INSERT INTO events (run_id, agent_id, event_type, step, data_json) "
        "VALUES (?, ?, 'agent_llm_response', 0, ?)",
        ("run-x", "agent-x", json.dumps(response_step0)),
    )
    con.commit()
    con.close()

    import tracing.server.app as app_mod
    monkeypatch.setattr(app_mod, "DEFAULT_DB_PATH", db_path)
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def client():
    from tracing.server.app import app
    return TestClient(app)


class TestMessagesEndpoint:

    def test_default_returns_json_array(self, client, db_with_step):
        """Back-compat: no `as=` param keeps the raw JSON shape so the
        existing UI fetch path doesn't break mid-deploy."""
        r = client.get("/api/runs/run-x/step/agent-x/0/messages")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        msgs = r.json()
        assert isinstance(msgs, list)
        assert msgs[0]["role"] == "system"

    def test_as_text_returns_rendered_transcript(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/0/messages?as=text")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        # Renderer signature lines + content from both messages.
        assert "SYSTEM" in body
        assert "USER" in body
        assert "You are a code reviewer." in body
        assert "Please review the diff." in body

    def test_as_text_404_is_plain_text(self, client, db_with_step):
        """Crucial for CLI piping: `quality-cli traces messages` writes
        the response straight to stdout, so a 404 must arrive as a
        plain message — not as `{"detail": "..."}` JSON the user would
        then have to unwrap."""
        r = client.get("/api/runs/run-x/step/agent-x/99/messages?as=text")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "(not found)"


class TestCallEndpoint:

    def test_default_returns_envelope(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/0/call")
        assert r.status_code == 200
        body = r.json()
        # Stable envelope shape the UI relies on.
        assert set(body.keys()) == {"content", "tool_calls"}
        assert body["tool_calls"][0]["function"]["name"] == "diff_read_file"

    def test_as_text_pretty_prints_tool_call_args(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/0/call?as=text")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        assert "▶ diff_read_file" in body
        # The args string was `{"path":"src/Foo.java","start_line":40}` —
        # the renderer must produce the indented pretty form, not the
        # original one-liner.
        assert '"path": "src/Foo.java"' in body
        assert '"start_line": 40' in body

    def test_as_text_404_plain(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/99/call?as=text")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "(not found)"


class TestToolsEndpoint:
    """`/api/runs/{id}/step/{agent}/{n}/tools` — surfaces the tool
    list the LLM saw at step N. Primary debugging use case:
    confirming a specific function (e.g. `agent_spawn`,
    `set_review_status`) was actually present in the request, and
    reading the description / parameter schema as the agent saw it."""

    def test_default_returns_envelope_with_full_schemas(self, client, db_with_step):
        """JSON shape: `{tools: [...openai-shape], tools_count}`. The
        envelope is stable — UI introspects `.tools[i].function.name`
        + `.function.description` directly."""
        r = client.get("/api/runs/run-x/step/agent-x/0/tools")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"tools", "tools_count"}
        assert body["tools_count"] == 2
        names = [t["function"]["name"] for t in body["tools"]]
        assert names == ["diff_read_file", "agent_spawn"]
        # Full schema landed — not just names.
        assert body["tools"][1]["function"]["description"].startswith("Delegate")

    def test_as_text_lists_names_descriptions_and_params(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/0/tools?as=text")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        # Banner counts what's available so a quick eyeball confirms
        # "yes, two tools were on the table".
        assert "TOOLS (2 available)" in body
        # Each tool surfaces its name + description.
        assert "▶ diff_read_file" in body
        assert "Read a slice of a file in the diff view." in body
        assert "▶ agent_spawn" in body
        assert "Delegate a focused investigation" in body
        # Required parameters are marked with `*`, optional with ` `.
        assert "* agent_name: string" in body
        assert "* focus: string" in body
        assert "* path: string" in body            # required
        assert "  start_line: integer" in body    # not required

    def test_legacy_event_only_count_renders_hint(self, client, db_with_step):
        """Pre-feature trace events stored only `tools_count` and
        dropped the full array. The endpoint must not crash on those
        — JSON returns `tools=[]` with the historical count, text
        view surfaces a clear hint instead of silently saying
        "0 tools" which would mislead a debugger reading an old run."""
        r = client.get("/api/runs/run-x/step/agent-x/1/tools")
        assert r.status_code == 200
        body = r.json()
        assert body["tools"] == []
        assert body["tools_count"] == 7

        t = client.get("/api/runs/run-x/step/agent-x/1/tools?as=text")
        assert t.status_code == 200
        assert "no tool schemas captured" in t.text
        assert "tools_count=7" in t.text

    def test_as_text_404_plain(self, client, db_with_step):
        r = client.get("/api/runs/run-x/step/agent-x/99/tools?as=text")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "(not found)"


class TestRenderToolsDirect:
    """`render_tools()` is also called from non-HTTP contexts (e.g. a
    future CLI command); pin its return shape so refactors don't
    silently break it."""

    def test_empty_with_no_legacy_count(self):
        from tracing.server.messages_render import render_tools
        out = render_tools([])
        assert "no tools" in out and "text-only" in out

    def test_empty_with_legacy_count_emits_hint(self):
        from tracing.server.messages_render import render_tools
        out = render_tools([], tools_count=5)
        assert "no tool schemas captured" in out
        assert "tools_count=5" in out

    def test_malformed_entry_doesnt_crash(self):
        """A non-dict tool entry shouldn't bring down rendering;
        we render a placeholder so the other tools are still readable."""
        from tracing.server.messages_render import render_tools
        out = render_tools([None, "garbage", {"function": {"name": "ok"}}])
        # Two malformed placeholders + the real one.
        assert "malformed entry" in out
        assert "▶ ok" in out
