"""
Production prompts MUST NOT contain symbols that the bench fixtures
grade agents on.

A leak: production reviewer/investigator/dispatcher prompt names
selectFreeItem / cancelOrder / @OneToMany / etc., the bench fires
those exact scenarios at the agent, the agent reflects on the symbol
the prompt already mentioned, the judge scores "found symbol" —
circular pass.

Found in the wild before this test existed:
  diffgraph/prompts/reviewer.user.md     — "Does selectFreeItem
                                            actually pick the
                                            cheapest item per the
                                            AC?" (REV-001 concern #1)
  diffgraph/prompts/reviewer.user.md     — focus="...null check for
                                            order.getItems() in
                                            cancelOrder..."
                                            (REV-U-002 / INV-U-001
                                            concern)
  diffgraph/prompts/reviewer.system.md   — "AGENTS.md says the free
                                            item is the cheapest,
                                            not group.get(0)"
                                            (REV-001 / REV-U-003)
  diffgraph/prompts/investigator.system.md — same line

The test self-derives the forbidden symbol list from the bench's
unit-fixture catalogue, so adding new fixtures auto-extends coverage
without touching this file.

Judge prompts are EXEMPT — the judge reads them, the agent doesn't,
so a fixture-symbol example in a judge prompt doesn't contaminate
the signal we're measuring.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


_DIFFGRAPH_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = _DIFFGRAPH_ROOT / "diffgraph" / "prompts"

# Same vocabulary allowlist as the bench-side leak check. These are
# the generic words of code review — a prompt can legitimately
# explain "transactions matter" without naming any fixture.
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
    # Additional words common in prompts but generic enough
    "auth check", "belong", "double-redeem",
    # Conventions-doc filenames — the prompts legitimately teach
    # agents to look for these, and fixtures legitimately grade on
    # citing them. Both directions are intentional, not leaks.
    # CURSOR_RULES.md, CONVENTIONS.md, CONTRIBUTING.md, etc. would
    # belong here too if any fixture started using them.
    "agents.md", "conventions.md", "contributing.md",
    # Prose abbreviations — contain a dot so `_is_fixture_specific`
    # would otherwise flag them as code-shaped tokens.
    "e.g", "i.e",
}


def _bench_root() -> Path | None:
    """Locate the bench tree — the directory that directly contains
    `scenarios/`. It's a subtree of diff-graph now (`benchmarks/`),
    so the default is just the in-repo path. The $BENCHMARK_REPO env
    override is kept for unusual layouts; it may point either at the
    `benchmarks/` dir itself or at a repo root that contains it.

    Returns None when not found — the test should skip rather than
    crash, since the bench subtree could theoretically be absent
    from a partial checkout.
    """
    env = os.environ.get("BENCHMARK_REPO", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        for cand in (p, p / "benchmarks"):
            if (cand / "scenarios" / "unit").exists():
                return cand
    in_repo = _DIFFGRAPH_ROOT / "benchmarks"
    if (in_repo / "scenarios" / "unit").exists():
        return in_repo
    return None


# Tokenise on whitespace and a small set of prose punctuation. We
# DO keep '.', '(', ')', '@', '[', ']', '=', '{', '}' inside tokens
# because those are the code-shape signals `_is_fixture_specific`
# looks for ("order.getItems()", "@OneToMany", etc.). Strip trailing
# prose punctuation (commas, colons, quotes) per-token.
_TOKEN_SPLIT_RE = re.compile(r"[\s,;:!?\"'`/\\|<>]+")
# We keep '(' ')' '[' ']' '{' '}' '@' '.' '=' INSIDE tokens because
# those are the code-shape markers `_is_fixture_specific` looks for
# (`getItems()`, `@OneToMany`, `order.items`). But we strip them at
# the EDGES so prose like "(any phrasing …)" → "any", "phrasing"
# instead of "(any", "phrasing)" — bare parens and trailing dots
# aren't identifiers.
_TOKEN_STRIP_CHARS = ".,;:!?\"'`-()[]{}"


def _tokenise(text: str) -> list[str]:
    """Split prose into candidate tokens. We don't try to be
    grammatically perfect — `_is_fixture_specific` already filters
    out anything that doesn't look like a code identifier, so over-
    tokenisation is harmless (false candidates get dropped) but
    under-tokenisation would silently miss leaks."""
    out: list[str] = []
    for raw in _TOKEN_SPLIT_RE.split(text or ""):
        tok = raw.strip(_TOKEN_STRIP_CHARS)
        if tok:
            out.append(tok)
    return out


def _walk_prose(node, sink: list[str]) -> None:
    """Recursively collect every string under `expected_output` —
    rationale blocks, must_mention/forbidden_topics list items,
    forbidden_comments[].description, etc. Anything text-shaped
    becomes a leak-detection corpus."""
    if isinstance(node, str):
        sink.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_prose(v, sink)
    elif isinstance(node, list):
        for v in node:
            _walk_prose(v, sink)


def _collect_fixture_keywords(bench_root: Path) -> dict[str, list[Path]]:
    """All fixture-side prose tokens across every unit fixture's
    `expected_output` block (rationale, must_mention, forbidden_topics,
    descriptions…), mapped to the fixture file(s) that introduced
    them. The map keys are LOWERCASED so the substring check is
    case-insensitive.

    Previously this read `description_keywords` arrays, but the
    semantic-asserts migration replaced those with prose. The leak
    guard still applies: if the rationale grading text names a
    specific symbol (`selectFreeItem`, `@OneToMany`, etc.), that
    symbol must not appear in the agent's prompt — otherwise the
    agent "knows" what to look for."""
    import yaml as yaml_lib
    out: dict[str, list[Path]] = {}
    unit_dir = bench_root / "scenarios" / "unit"
    for yp in unit_dir.rglob("*.yaml"):
        raw = yaml_lib.safe_load(yp.read_text(encoding="utf-8")) or {}
        eo = raw.get("expected_output") or {}
        prose: list[str] = []
        _walk_prose(eo, prose)
        for chunk in prose:
            for tok in _tokenise(chunk):
                out.setdefault(tok.lower(), []).append(yp)
    return out


def _agent_prompt_files() -> list[Path]:
    """Every agent-side prompt: reviewer/investigator/dispatcher.
    Judges are explicitly EXCLUDED — they see the keywords, the
    agent doesn't, so a fixture symbol in a judge prompt isn't a
    contamination of the agent signal."""
    if not _PROMPTS_DIR.exists():
        return []
    files = []
    for p in _PROMPTS_DIR.rglob("*.md"):
        # Skip judge prompts — they're the grader, not the gradee.
        if "judges" in p.parts:
            continue
        files.append(p)
    return files


def _is_fixture_specific(keyword: str) -> bool:
    """Filter to identifiers we expect to be unique to one fixture —
    skip generic vocabulary that legitimately appears in prompts.

    A keyword is "fixture-specific" if it:
      - contains a CamelCase identifier (StoreCredit, OrderService), OR
      - contains a code char (parens, dots, @, brackets), OR
      - is a multi-word phrase with at least one CamelCase /
        code-charred token (e.g. "null guard order.getItems")

    Single common words (cheapest, ownership, transaction) are
    skipped here because they may legitimately appear in prompts —
    those overlaps are caught by the bench-side leak test instead.
    """
    if not keyword:
        return False
    if keyword in _GENERIC_VOCABULARY:
        return False
    # Code chars — parens, dots, @, brackets, equals
    if re.search(r"[(){}\[\]@=.]", keyword):
        return True
    # CamelCase / camelCase — letter then lower then upper
    if re.search(r"[a-z][A-Z]", keyword):
        return True
    # ALLCAPS abbreviation (IDOR, NPE) longer than 2 letters
    if re.fullmatch(r"[A-Z]{3,}", keyword):
        return True
    return False


def test_no_fixture_specific_symbols_in_agent_prompts():
    """For every agent prompt file, no bench-fixture keyword that
    looks like a code identifier may appear verbatim."""
    bench = _bench_root()
    if bench is None:
        pytest.skip(
            "benchmarks/ subtree not found in this checkout; "
            "set BENCHMARK_REPO env var to enable this guard"
        )

    fixture_keywords = _collect_fixture_keywords(bench)
    prompts = _agent_prompt_files()
    assert prompts, f"no agent prompts found under {_PROMPTS_DIR}"

    forbidden = {
        kw: paths for kw, paths in fixture_keywords.items()
        if _is_fixture_specific(kw)
    }

    leaks: list[tuple[Path, str, list[Path]]] = []
    for prompt in prompts:
        text = prompt.read_text(encoding="utf-8").lower()
        for kw, fixture_paths in forbidden.items():
            if kw in text:
                leaks.append((prompt, kw, fixture_paths))

    if leaks:
        msg = ["\nFixture-specific symbols found in production prompts:"]
        for prompt, kw, fps in leaks:
            rel = prompt.relative_to(_DIFFGRAPH_ROOT)
            origins = ", ".join(
                str(fp.name) for fp in fps[:3]
            )
            msg.append(f"  • {rel}: {kw!r}  (graded by: {origins})")
        msg.append(
            "\nFix: rewrite the prompt to use a placeholder "
            "(FUNCTION_X / EDGE_CASE_Y / CONVENTIONS_DOC) instead of "
            "naming any specific class / method / file from the "
            "benchmark fixtures."
        )
        pytest.fail("\n".join(msg))


def test_fixture_keyword_collection_is_non_empty():
    """Smoke — if the glob returns zero fixtures, the leak guard
    runs over an empty forbidden-set and trivially passes. Catch
    that misconfiguration."""
    bench = _bench_root()
    if bench is None:
        pytest.skip("bench repo not located")
    keywords = _collect_fixture_keywords(bench)
    assert keywords, "no fixture keywords discovered — glob misconfigured?"
