"""`quality-cli traces messages|call` — the CLI consumes the API's
`?as=text` view and prints it verbatim to stdout.

These pins exist because the CLI is now expected to be a "dumb"
client: the renderer lives server-side and the CLI must NOT
reformat the response. If we ever add client-side massaging the
contract breaks and downstream tools that pipe `quality-cli traces
messages ... > step.txt` get surprised output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def stub_api(monkeypatch):
    """Replace the two API getters so the CLI tests don't need an
    actually-running uvicorn instance. The test records every call
    so we can also assert *which* endpoint variant the CLI hit (text
    vs JSON path)."""
    calls: list = []

    def fake_text(path, params=None):
        calls.append((path, params or {}, "text"))
        # Canned response — same shape `render_messages` would emit.
        return "━━━━━━\n  SYSTEM\n━━━━━━\nYou are a reviewer.\n"

    def fake_json(path, params=None):
        calls.append((path, params or {}, "json"))
        return [{"role": "system", "content": "You are a reviewer."}]

    import quality_cli.main as cli_mod
    monkeypatch.setattr(cli_mod, "_api_get_text", fake_text)
    monkeypatch.setattr(cli_mod, "_api_get", fake_json)
    return calls


class TestTracesMessagesCommand:

    def test_default_hits_text_endpoint(self, runner, stub_api):
        """`traces messages <run> <agent> <step>` with no `--as` flag
        defaults to the text view — the human-readable transcript
        emerges on stdout, no JSON wrapping."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "messages", "run-1", "agent-1", "0"])
        assert r.exit_code == 0, r.output
        # Server-side renderer output should land verbatim.
        assert "SYSTEM" in r.output
        assert "You are a reviewer." in r.output
        # And we called `_api_get_text` (text endpoint), not `_api_get`.
        kinds = [c[2] for c in stub_api]
        assert "text" in kinds and "json" not in kinds

    def test_as_json_hits_raw_endpoint(self, runner, stub_api):
        """`--as json` returns the raw OpenAI array. Tooling that
        wants to grep / re-render uses this path."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "messages", "run-1", "agent-1", "0",
                           "--as", "json"])
        assert r.exit_code == 0, r.output
        parsed = json.loads(r.output)
        assert isinstance(parsed, list)
        assert parsed[0]["role"] == "system"
        kinds = [c[2] for c in stub_api]
        assert "json" in kinds and "text" not in kinds

    def test_invalid_as_value_errors(self, runner, stub_api):
        """Defensive: typo on `--as` shouldn't silently fall through
        to one of the variants."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "messages", "run-1", "agent-1", "0",
                           "--as", "bogus"])
        # _emit_error path doesn't typer-raise but emits and returns.
        # The body should mention the bad arg either way; what we
        # really want is "didn't call either getter".
        assert stub_api == []

    def test_text_path_passes_as_text_param(self, runner, stub_api):
        """Sanity: the text-mode path explicitly forwards `as=text` so
        the server-side renderer is engaged. If we ever drop the
        param the CLI would silently start returning JSON."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "messages", "run-1", "agent-1", "0"])
        assert r.exit_code == 0
        text_calls = [c for c in stub_api if c[2] == "text"]
        assert text_calls, "no text-mode call recorded"
        _path, params, _ = text_calls[0]
        assert params.get("as") == "text"


class TestTracesCallCommand:
    """The /call endpoint follows the same `--as text|json` contract."""

    def test_default_text_view(self, runner, stub_api):
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "call", "run-1", "agent-1", "0"])
        assert r.exit_code == 0
        assert "SYSTEM" in r.output
        kinds = [c[2] for c in stub_api]
        assert kinds == ["text"]

    def test_json_view_returns_envelope(self, runner, stub_api):
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "call", "run-1", "agent-1", "0",
                           "--as", "json"])
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert isinstance(parsed, list)


class TestTracesBenchLogCommand:
    """`traces bench-log <target>` — pipes the API's combined text
    view to stdout for grepping / paging. Same `--as` and stream
    selector as the API endpoint."""

    def test_numeric_target_hits_task_endpoint(self, runner, stub_api):
        """Numeric argument is interpreted as task_id and routes to
        `/api/qa/tasks/{id}/bench-log`."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app, ["traces", "bench-log", "42"])
        assert r.exit_code == 0
        assert stub_api, "no API call recorded"
        path, _params, kind = stub_api[-1]
        assert path == "/api/qa/tasks/42/bench-log"
        assert kind == "text"

    def test_hex_target_hits_run_alias(self, runner, stub_api):
        """Non-numeric argument routes to the run-id alias."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app, ["traces", "bench-log", "abc123def456"])
        assert r.exit_code == 0
        path, _params, _kind = stub_api[-1]
        assert path == "/api/runs/abc123def456/bench-log"

    def test_stream_param_forwarded(self, runner, stub_api):
        """`--stream stderr` makes it to the request params verbatim
        — important because the API reads exactly that key."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "bench-log", "42", "--stream", "stderr"])
        assert r.exit_code == 0
        _path, params, _kind = stub_api[-1]
        assert params.get("stream") == "stderr"

    def test_as_json_uses_json_getter(self, runner, stub_api):
        """`--as json` routes through `_api_get` (parsed JSON) rather
        than `_api_get_text` so the user gets a properly-rendered
        JSON dump."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "bench-log", "42", "--as", "json"])
        assert r.exit_code == 0
        # The stub for _api_get returns a list[{role: ...}]. The
        # command should JSON-dump it. Sanity-check the output
        # parses back to the same shape.
        parsed = json.loads(r.output)
        assert isinstance(parsed, list)
        kinds = [c[2] for c in stub_api]
        assert "json" in kinds and "text" not in kinds

    def test_invalid_stream_short_circuits(self, runner, stub_api):
        """Typo on `--stream` shouldn't silently fetch with a bogus
        value — the API would 200 (because it falls back to combined
        for unknown stream) but the user's intent was something
        specific. Validate client-side."""
        from quality_cli.main import app as cli_app
        r = runner.invoke(cli_app,
                          ["traces", "bench-log", "42", "--stream", "bogus"])
        # _emit_error doesn't typer-raise but emits and returns;
        # what matters is that NO API call was made.
        assert stub_api == []
