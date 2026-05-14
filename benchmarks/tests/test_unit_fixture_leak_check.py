"""
Self-check for unit fixtures: the agent's input MUST NOT already
contain the keywords its expected_output grades against.

Why: a leak makes the score circular — "prompt told you about X →
agent reflected on X → judge scored 'found X' — pass". Earlier
versions of REV-U-002 and INV-U-001 hit exactly this trap; their
0.85 / 0.95 scores were partly the agent confirming what we'd
pre-told it.

Leak channels checked (everything the framework controls; NOT the
diff itself or AGENTS.md, which the agent has to discover):

  - user_message_from file content
  - agent_data.* values (e.g. investigator's focus)
  - pr_state.metadata.title + description
  - pr_state.comments[].text (seed thread visible via list_threads)
  - trigger.text

Per-fixture override: a yaml may declare `leak_allowlist: [...]` to
whitelist specific overlaps that are unavoidable (e.g. an
identifier that legitimately has to appear in BOTH the PR
description and the expected concern wording). Keep this list
short and motivated — every entry is something the test author
manually decided "not really a leak".
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest

# Generic vocabulary words that legitimately appear in both inputs
# and concern wording. These are the language of code review, not
# scenario-specific identifiers — "concurrent", "race", "verify" can
# show up in any reasonable description of any reasonable PR.
# When a real leak hides behind a generic word (e.g. the PR title
# spells out "race condition"), that's caught by SOMETHING ELSE
# in the keyword group also appearing — keyword groups are AND-of-
# OR so the test still flags as long as one non-generic term hits.
_GENERIC_VOCABULARY = {
    "rule", "convention", "guideline",
    "race", "lock", "concurrent", "atomic",
    "transaction", "ownership", "authorization", "verify",
    "concern", "partial", "leftover", "remainder", "amount",
    "deleted", "removed", "fix", "hide", "real", "bug",
    "symptom", "cause", "upstream", "consumer", "entity",
    "boundary", "invariant", "construct", "scope", "import",
    "gradle", "wrapper", "build", "convention", "unrelated",
    "minor", "major", "blocker",
}

_BENCH_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_GLOB = str(_BENCH_ROOT / "scenarios" / "unit" / "**" / "*.yaml")


def _all_fixtures():
    return sorted(glob.glob(_FIXTURE_GLOB, recursive=True))


def _collect_agent_inputs(yaml_path: Path) -> str:
    """Concat everything the agent reads from framework-controlled
    inputs (NOT the diff, NOT AGENTS.md). Lower-cased so the
    substring check is case-insensitive."""
    import yaml as yaml_lib
    raw = yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    parts: list[str] = []

    # user_message_from (concerns-only.md or similar)
    umf = raw.get("user_message_from")
    if umf:
        p = (yaml_path.parent / str(umf)).resolve()
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))

    # agent_data.{focus,…}
    for v in (raw.get("agent_data") or {}).values():
        parts.append(str(v))

    # pr_state.metadata.{title, description}
    md = (raw.get("pr_state") or {}).get("metadata") or {}
    parts.append(str(md.get("title", "")))
    parts.append(str(md.get("description", "")))

    # pr_state.comments[].text — seed threads
    for c in ((raw.get("pr_state") or {}).get("comments") or []):
        parts.append(str(c.get("text", "")))

    # trigger.text
    parts.append(str((raw.get("trigger") or {}).get("text", "")))

    return "\n".join(parts).lower()


def _collect_expected_keywords(yaml_path: Path) -> list[tuple[str, str, str]]:
    """Returns [(source, group_id, keyword), ...] for every
    description_keyword in expected_output. `source` tags the
    channel (concern_focuses / required_comments) so the failure
    message tells you what's leaking."""
    import yaml as yaml_lib
    raw = yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    eo = raw.get("expected_output") or {}
    out: list[tuple[str, str, str]] = []
    for cf in (eo.get("concern_focuses") or []):
        gid = str(cf.get("id") or "<unnamed>")
        for kw in (cf.get("description_keywords") or []):
            out.append(("concern_focuses", gid, str(kw)))
    for rc in (eo.get("required_comments") or []):
        gid = str(rc.get("id") or "<unnamed>")
        for kw in (rc.get("description_keywords") or []):
            out.append(("required_comments", gid, str(kw)))
    # reply.must_mention is allowed to overlap with PR title etc. —
    # by design, the dispatcher's ack quotes the PR title and the
    # judge matches on that quote. Skip it for leak-checking.
    return out


def _allowlist(yaml_path: Path) -> set[str]:
    import yaml as yaml_lib
    raw = yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return {str(x).lower() for x in (raw.get("leak_allowlist") or [])}


@pytest.mark.parametrize("fixture_path", _all_fixtures())
def test_unit_fixture_input_has_no_expected_keyword_leak(fixture_path):
    """For each fixture, no expected_output keyword may appear
    verbatim in the agent's framework-controlled input."""
    p = Path(fixture_path)
    inputs = _collect_agent_inputs(p)
    keywords = _collect_expected_keywords(p)
    allow = _allowlist(p)

    leaks: list[tuple[str, str, str]] = []
    for source, group_id, kw in keywords:
        kw_l = kw.lower().strip()
        if not kw_l:
            continue
        if kw_l in _GENERIC_VOCABULARY:
            continue
        if kw_l in allow:
            continue
        # Substring match — most code identifiers and multi-word
        # phrases are too specific to false-positive on. Generic
        # vocabulary is filtered above; if you find an edge case,
        # add it to _GENERIC_VOCABULARY or the per-fixture
        # leak_allowlist.
        if kw_l in inputs:
            leaks.append((source, group_id, kw))

    if leaks:
        rel = p.relative_to(_BENCH_ROOT)
        msg = [f"\n{rel}: expected-keyword leaks in agent input:"]
        for source, gid, kw in leaks:
            msg.append(f"  • {source}.{gid}: {kw!r}")
        msg.append(
            "\nFix: rewrite the input (concerns-only.md, focus, "
            "pr_state metadata, or trigger text) so the keyword "
            "doesn't appear, OR add to leak_allowlist in the yaml "
            "if the overlap is structurally unavoidable."
        )
        pytest.fail("\n".join(msg))


def test_leak_check_has_at_least_one_fixture():
    """Smoke — if globbing breaks, the parametrize above yields zero
    cases and pytest reports 'passed' misleadingly. This guards
    against silent skip."""
    assert len(_all_fixtures()) > 0, (
        "No unit fixtures discovered under scenarios/unit/ — "
        "leak check is silently not running."
    )
