"""
YAML frontmatter parser for prompt-side `*.user.md` files.

Standard frontmatter format::

    ---
    dispatch_mode: native | meta            # how tools are exposed to the LLM
    tools: [name1, name2, ...]              # full replace: agent's runtime
                                             # tool set is exactly this list.
                                             # Use when the prompt fully
                                             # specifies its needed surface.
    tools_add: [name1, name2, ...]          # EXTENSION over default @tools.
                                             # Use when the prompt wants
                                             # everything the agent normally
                                             # has plus a few extras (e.g.
                                             # bring in submit_answer for a
                                             # test without restating the
                                             # full default toolkit).
                                             # Mutually exclusive with tools.
    extra_tools:                            # additional capture-style tools
      - name: submit_answer                  # registered into the registry
        description: "Submit your final text. Call once."
        parameters:
          type: object
          properties:
            text: {type: string}
          required: [text]
    ---
    PR: {pr_title}
    ...
    <task wording>

The body (text after the closing `---`) is what the LLM actually sees as
the user message. The frontmatter never reaches the model — it's metadata
the orchestration layer reads to (a) filter the tool registry per-run,
(b) register extra capture-style tools, (c) choose the dispatch strategy.

Why a parser separate from yaml.safe_load over the whole file: the body
contains `{placeholders}` (Jinja-ish, not YAML) and free prose, so the
file is NOT yaml. The standard convention — `---` block at the top
followed by a `---` close, then arbitrary text — is the convention
Jekyll / Hugo / etc. settled on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


# Markers MUST appear on their own line at the very start of the file
# (frontmatter) and the closing fence. Leading whitespace permitted on
# either fence to match the relaxed Jekyll/Hugo convention.
_FENCE = "---"


@dataclass
class Frontmatter:
    """Parsed metadata + body. Empty meta + full text on no-frontmatter
    input — keeps callers branch-free."""
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse(text: str) -> Frontmatter:
    """Split text into (frontmatter dict, body). When the file has no
    frontmatter — Frontmatter(meta={}, body=text). When the file starts
    with `---` but the YAML is malformed — raises ValueError (strict;
    silent-skip would let prompt authors typo themselves into
    production-default behavior, which is exactly the regression risk
    the frontmatter is supposed to remove)."""
    if not text:
        return Frontmatter()

    # Strip a leading BOM / single leading newline so files saved by
    # editors with quirks still parse cleanly. Don't aggressively
    # left-trim — we need to see the first line as-is to detect the
    # fence.
    src = text.lstrip("﻿")
    if src.startswith("\n"):
        src = src[1:]

    if not src.startswith(_FENCE):
        return Frontmatter(meta={}, body=text)

    # Find the closing fence. The opening fence is line 1; look for
    # the next standalone `---` line.
    lines = src.split("\n")
    # lines[0] == "---" (or "--- ..." but we'll be strict — pure fence)
    if lines[0].strip() != _FENCE:
        return Frontmatter(meta={}, body=text)

    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(
            "frontmatter: opening `---` fence has no matching close. "
            "Add a closing `---` line after the YAML block."
        )

    yaml_text = "\n".join(lines[1:close_idx])
    body_text = "\n".join(lines[close_idx + 1:]).lstrip("\n")

    try:
        meta = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter: invalid YAML — {exc}") from exc

    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(
            f"frontmatter: top-level must be a mapping, got {type(meta).__name__}"
        )

    return Frontmatter(meta=meta, body=body_text)


def split_path(path: str | "PathLike") -> Frontmatter:
    """Convenience: read a file from disk and parse its frontmatter."""
    from pathlib import Path
    return parse(Path(path).expanduser().read_text(encoding="utf-8"))


# ── Schema validation for the standard fields ────────────────────────────


_VALID_DISPATCH_MODES = {"native", "meta"}


def validate(fm: Frontmatter) -> None:
    """Strict validation of known fields. Unknown fields are allowed
    (forward-compat: future fields can land without breaking older
    parsers) but the known fields must have well-formed shapes.

    Raises ValueError with a precise message on any violation. Authors
    fix the prompt, run it again. No silent fallback to "production
    defaults" — that would mask exactly the misconfiguration risk
    frontmatter is here to eliminate.
    """
    meta = fm.meta
    if not meta:
        return

    if "dispatch_mode" in meta:
        dm = meta["dispatch_mode"]
        if not isinstance(dm, str) or dm not in _VALID_DISPATCH_MODES:
            raise ValueError(
                f"frontmatter.dispatch_mode: expected one of "
                f"{sorted(_VALID_DISPATCH_MODES)}, got {dm!r}"
            )

    for field_name in ("tools", "tools_add"):
        if field_name not in meta:
            continue
        t = meta[field_name]
        if not isinstance(t, list):
            raise ValueError(
                f"frontmatter.{field_name}: expected list of strings, "
                f"got {type(t).__name__}"
            )
        for i, n in enumerate(t):
            if not isinstance(n, str) or not n.strip():
                raise ValueError(
                    f"frontmatter.{field_name}[{i}]: expected non-empty "
                    f"string, got {n!r}"
                )

    if "tools" in meta and "tools_add" in meta:
        raise ValueError(
            "frontmatter: `tools` (full replace) and `tools_add` "
            "(additive) are mutually exclusive — pick one."
        )

    if "extra_tools" in meta:
        et = meta["extra_tools"]
        if not isinstance(et, list):
            raise ValueError(
                "frontmatter.extra_tools: expected list of "
                "{name, description, parameters} maps"
            )
        for i, spec in enumerate(et):
            if not isinstance(spec, dict):
                raise ValueError(
                    f"frontmatter.extra_tools[{i}]: expected mapping, "
                    f"got {type(spec).__name__}"
                )
            for field_name in ("name", "description", "parameters"):
                if field_name not in spec:
                    raise ValueError(
                        f"frontmatter.extra_tools[{i}]: missing required "
                        f"field {field_name!r}"
                    )
            if not isinstance(spec["name"], str) or not spec["name"].strip():
                raise ValueError(
                    f"frontmatter.extra_tools[{i}].name: must be a non-empty string"
                )
            if not isinstance(spec["parameters"], dict):
                raise ValueError(
                    f"frontmatter.extra_tools[{i}].parameters: must be a "
                    f"JSON-schema-shaped mapping"
                )
