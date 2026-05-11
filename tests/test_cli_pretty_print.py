"""
`bench run-unit` pretty-prints the agent's structured output to the
console. The print path must not crash on agent outputs containing
long string values.

Background: the old code was

    console.print_json(json.dumps(result.cli_output, default=str)[:2000])

The `[:2000]` slice can cut a string literal mid-value, leaving an
unterminated `"`. rich's `console.print_json` then calls `loads()`
on the truncated string and raises
`JSONDecodeError: Unterminated string starting at: line 1 column N`,
which bubbles all the way up and exits the CLI with code 1 — even
though the underlying agent run passed. That false-negative confused
the queue (`non_zero_exit` retries) and hid investigator findings on
fixtures whose output happened to be larger than the slice threshold.

Spotted on plan 106 / task 1368 (INV-U-001-cancel-npe) after the
fake-PR plumbing fix unblocked investigator fixtures and they
started producing real, long findings for the first time.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest


@pytest.fixture
def fat_cli_output():
    """Agent output shaped like the investigator JSON contract:
    a list of finding dicts with string fields large enough that
    a naive 2000-char slice of the JSON dump WILL cut mid-string."""
    long_explanation = "x" * 3000   # > 2000-char threshold by itself
    return [
        {
            "file": "src/Foo.java",
            "line": 42,
            "severity": "BLOCKER",
            "title": "test title",
            "explanation": long_explanation,
            "evidence": "AGENTS.md says " + ("y" * 1500),
            "suggestion": "fix it",
        },
        {
            "file": "src/Bar.java",
            "line": 99,
            "severity": "MAJOR",
            "title": "second title",
            "explanation": "z" * 2000,
            "evidence": "shorter",
            "suggestion": "and another",
        },
    ]


def _captured_console():
    """Headless rich console that records all output to an in-memory
    buffer (no real terminal needed)."""
    from rich.console import Console
    return Console(file=StringIO(), force_terminal=False, width=120)


def test_old_slice_pattern_fails_on_long_output(fat_cli_output):
    """Pin the bug: the old `json.dumps(...)[:2000]` pattern DOES
    raise on our fat output. If this ever stops raising it means
    the slice threshold drifted or the test no longer exercises
    the failure mode — re-tune `long_explanation` size."""
    console = _captured_console()
    truncated = json.dumps(fat_cli_output, default=str)[:2000]
    with pytest.raises(json.JSONDecodeError):
        console.print_json(truncated)


def test_print_json_data_kwarg_handles_long_output(fat_cli_output):
    """The fix: pass the dict via `data=` so rich serialises it
    internally. No slice ⇒ no truncated quote ⇒ no
    JSONDecodeError. The full output lands in the buffer."""
    console = _captured_console()
    console.print_json(data=fat_cli_output, default=str)
    output = console.file.getvalue()
    # The serialised JSON is in there, intact.
    assert '"file"' in output
    assert "src/Foo.java" in output
    assert "src/Bar.java" in output
    # And the long explanation made it through, not silently chopped.
    assert "x" * 100 in output  # spot-check a chunk of the long field


def test_cli_pretty_print_path_does_not_raise(fat_cli_output, monkeypatch, capsys):
    """End-to-end: drive the exact code path in `cli.py:run_unit`
    that used to crash. We patch `run_unit_fixture` to return a
    `UnitRunResult` with our fat `cli_output`, then call the typer
    handler and assert it returns cleanly."""
    from runner.run_unit import UnitRunResult
    from pathlib import Path
    import benchmark.cli as bench_cli

    fake_result = UnitRunResult(
        fixture_id="INV-U-TEST",
        agent="investigator",
        exit_code=0,
        cli_output=fat_cli_output,
        stdout_tail="",
        stderr_tail="",
        base_sha="aaaaaaaaaaaa",
        source_sha="bbbbbbbbbbbb",
        tmp_repo=Path("/tmp/fake"),
        posted=[],
    )
    # `cli.run_unit` imports `run_unit_fixture` lazily inside the
    # function body, so patch where the name actually resolves.
    import runner.run_unit
    monkeypatch.setattr(runner.run_unit, "run_unit_fixture",
                        lambda *_a, **_kw: fake_result)
    # cli.py:run_unit checks `p.exists()` before invoking the runner;
    # point it at a real fixture file so we sail through that gate.
    real_fixture = (Path(__file__).resolve().parents[1] / "scenarios"
                    / "unit" / "investigator" / "INV-U-001-cancel-npe.yaml")
    # The handler is a typer command — call its underlying function.
    fn = bench_cli.run_unit
    if hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    # No SystemExit / exception should fire.
    fn(fixture=str(real_fixture), provider="deepseek",
       timeout=60, keep_tmp=False)
