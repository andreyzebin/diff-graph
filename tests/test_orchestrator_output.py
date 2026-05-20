"""Regression tests for `run_agent`'s output normalisation.

The bug: `answer(text=...)` delivers a single text payload, carried
internally as `[{"text": ...}]`. `run_agent` used to collapse ANY
list to `{"findings": [...]}`, so cli.py's `--output` writer pushed
the text payload through `_parse_findings` (which requires a `file`
key) and silently dropped it — the judge subprocess then wrote `[]`
and the bench scored nothing. `_normalize_run_output` keeps the
answer payload distinct.
"""
from __future__ import annotations

from diffgraph.orchestrator import _normalize_run_output


def test_answer_payload_surfaces_as_text() -> None:
    """The answer() shape [{"text": X}] becomes {"text": X} — never a
    findings array."""
    out = _normalize_run_output([{"text": '{"overall_score": 0.9}'}])
    assert out == {"text": '{"overall_score": 0.9}'}


def test_findings_list_stays_findings() -> None:
    """A real done(findings=[...]) list (finding dicts) still maps to
    {"findings": [...]}."""
    findings = [{"file": "a.py", "line": 1, "severity": "MAJOR"}]
    assert _normalize_run_output(findings) == {"findings": findings}


def test_multi_item_text_list_is_not_an_answer() -> None:
    """Only a one-element [{"text": …}] is the answer shape; anything
    else is treated as findings."""
    two = [{"text": "a"}, {"text": "b"}]
    assert _normalize_run_output(two) == {"findings": two}
    # A one-element dict with extra keys is not the answer wrapper.
    mixed = [{"text": "a", "file": "x.py"}]
    assert _normalize_run_output(mixed) == {"findings": mixed}


def test_dict_and_none_passthrough() -> None:
    assert _normalize_run_output({"verdict": "pass"}) == {"verdict": "pass"}
    assert _normalize_run_output(None) == {}


def test_json_string_parsed() -> None:
    assert _normalize_run_output('{"verdict": "pass"}') == {"verdict": "pass"}
    assert _normalize_run_output('[{"file": "a.py"}]') == {
        "findings": [{"file": "a.py"}]}
    assert _normalize_run_output("not json") == {"text": "not json"}
