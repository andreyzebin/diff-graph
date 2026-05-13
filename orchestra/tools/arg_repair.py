"""Per-tool-call argument repair handlers.

`ToolRegistry.dispatch` walks this chain in order **only when**
schema validation has already failed for a tool call. Each handler
proposes a repaired args dict; the framework re-validates the
proposal and commits the first one that passes. Returning `None`
means "I don't recognise this failure shape — defer to the next
handler."

Why a chain (not a single repair step):

- Each handler stays narrow. It fixes exactly one observed failure
  shape and refuses to commit anything that doesn't actually solve
  the validation error. Adding a third handler later doesn't risk
  masking other failure modes.
- Repairs **never run preemptively**. A tool call that passes
  validation on its own goes straight through — no regex scan, no
  guesswork on otherwise-fine payloads.
- The trigger is the validation error itself ("X is a required
  property"). That's the only signal we know is safe to act on:
  the framework is telling us a specific field the schema needed
  but the args don't have. If the repair recovers it, great;
  otherwise we leave the original error to surface to the model.

Handler order in the default qwen3 chain:

1. TruncatedJsonHandler — operates on the RAW arguments string.
   Cheapest to check (regex pass + json.loads). Fires only when
   the raw parsed to {} (i.e. the model emitted a syntax-level
   bug). Skips when args came in non-empty (parse worked, so
   truncated-key repair has nothing to do).

2. StringifiedArgsHandler — operates on the already-parsed dict.
   Walks string-typed properties looking for nested JSON whose
   keys cover the missing-required set.

Caller adds custom handlers by passing
`ToolRegistry(arg_repair_handlers=[...])`. The two built-ins
are otherwise wired via `fix_qwen3_stringification_bug=True`.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# The jsonschema error message we look for to decide a handler should
# even try. We match defensively across phrasings — jsonschema's
# wording has wobbled across versions ("is a required property",
# "required property '<name>'", "'X' is required"). A `re.search`
# (not match) so the location prefix `validation error at '...':`
# doesn't bother us.
_MISSING_REQUIRED_RE = re.compile(
    r"is a required property|required property|is required"
)


def _is_missing_required(error: str) -> bool:
    """True when the validation error reports a missing required
    field. Handlers gate themselves on this — repair is only
    attempted when the framework specifically flagged "you don't
    have what the schema wanted." Any other validation error
    (wrong type, enum violation, etc.) is the model's problem to
    fix; we don't try to second-guess."""
    return bool(error and _MISSING_REQUIRED_RE.search(error))


@runtime_checkable
class ArgRepairHandler(Protocol):
    """One repair attempt in the dispatch chain.

    Implementors must:
      - Return `None` when this handler's failure shape doesn't match
        the call signature (so the chain moves on).
      - Return a candidate `dict` otherwise. The framework will
        re-validate it against the same schema; if validation still
        fails, the framework calls the next handler with the
        ORIGINAL args (not the failed candidate) — handlers see
        the same baseline.
    """
    name: str

    def try_repair(
        self,
        *,
        raw_args: Optional[str],
        args: dict,
        schema: dict,
        error: str,
    ) -> Optional[dict]:
        ...


class TruncatedJsonHandler:
    """Recover from the qwen3 `"key": }` / `"key": ,` failure mode.

    Pre-conditions for firing:
      - `error` is a missing-required validation error
      - `args` is empty (parse failed → fell back to {})
      - `raw_args` is a non-trivial string we can repair

    These three together are the signature of "model emitted broken
    JSON, our parser silently lost everything, validation is
    reporting the first required key as missing — but the model
    might have actually included it, just inside a malformed
    payload." If the regex repair turns the raw string into a
    parseable object, we hand that back; the framework re-validates
    and commits only if it now passes.

    A repair that doesn't change the raw string (no truncated keys
    were present, the parse failure was for some other reason)
    returns None so the next handler can try.
    """

    name = "truncated_json"

    def try_repair(self, *, raw_args, args, schema, error):
        # Missing-required gate is enforced by the chain harness in
        # `ToolRegistry.dispatch` — this handler trusts that and only
        # checks its own precondition: args is empty (parse fell back).
        # Parse succeeded → there's nothing to repair at the syntax
        # level. The stringified-args handler runs next; that's where
        # nested JSON gets lifted.
        if args:
            return None
        if not isinstance(raw_args, str) or not raw_args.strip():
            return None
        from orchestra.tools.registry import _repair_truncated_json
        repaired = _repair_truncated_json(raw_args)
        if repaired == raw_args:
            return None  # nothing matched — not our failure shape
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


class OverEscapedKeysHandler:
    """Recover from the qwen3 "over-escaped top-level keys" shape
    (plan 218 run cbb7a22ec16d step 6).

    Wild-type: model emits `\\"X\\":` for top-level object keys
    instead of `"X":`. JSON parser sees `\\"` as an escaped quote
    (literal `"` inside the string value), so the FIRST value
    string never closes → "Unterminated string starting at N". The
    agent loop's parse fallback turns args into `{}`, validation
    reports the first required key as missing.

    Pre-conditions for firing:
      - `error` is a missing-required validation error
      - `args` is empty (the over-escape made the FIRST value
        string consume the entire payload)
      - `raw_args` contains `\\"` sequences (cheap reject;
        otherwise nothing to un-escape)

    Repair: `_repair_over_escaped_keys` globally replaces `\\"`
    with `"` in the raw text. If the result parses to a dict
    covering the missing-required, the framework commits it.

    Ordered AFTER `TruncatedJsonHandler` in the chain because
    truncated-key repair is a narrower (regex-bounded) transform;
    if both shapes could plausibly apply, the narrower one is the
    safer first attempt. In practice their preconditions are
    disjoint (truncated → some structural empty pair; over-escaped
    → `\\"` in raw), so order is more about consistency than
    correctness."""

    name = "over_escaped_keys"

    def try_repair(self, *, raw_args, args, schema, error):
        # Missing-required gate enforced by the chain harness.
        # Precondition: args came in empty (parse fell back).
        if args:
            return None
        if not isinstance(raw_args, str) or not raw_args.strip():
            return None
        if '\\"' not in raw_args:
            return None  # nothing to un-escape
        from orchestra.tools.registry import _repair_over_escaped_keys
        repaired = _repair_over_escaped_keys(raw_args)
        if repaired == raw_args:
            return None
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


class StringifiedArgsHandler:
    """Recover from the qwen3 "all args packed into one escaped-JSON
    string" failure mode (qwen-code#379).

    Pre-conditions for firing:
      - `error` is a missing-required validation error
      - `args` is a non-empty dict (parse succeeded, but one of its
        string values contains the keys the schema expected)

    The lift logic itself lives in `_repair_stringified_args`. This
    wrapper just gates it on the missing-required signal so a
    fully-formed payload with a different kind of validation error
    (wrong enum, type mismatch) never triggers the regex/parse
    overhead.
    """

    name = "stringified_args"

    def try_repair(self, *, raw_args, args, schema, error):
        # Missing-required gate is enforced by the chain harness.
        # Precondition here: args parsed to something non-empty.
        # An empty args dict means parse failed — the truncated-json
        # handler that ran before us would have already handled it.
        if not isinstance(args, dict) or not args:
            return None
        from orchestra.tools.registry import _repair_stringified_args
        return _repair_stringified_args(args, schema)


def default_qwen3_chain() -> list[ArgRepairHandler]:
    """The three-handler chain enabled by
    `ToolRegistry(fix_qwen3_stringification_bug=True)`. Order
    matters:

      1. TruncatedJson  — narrow regex over `"key": ,` / `"key": }`
                          (precondition: args empty)
      2. OverEscapedKeys — global `\\"`→`"` un-escape
                          (precondition: args empty + `\\"` in raw)
      3. StringifiedArgs — lift nested JSON from a string property
                          (precondition: args non-empty)

    First two share the `args empty` precondition so order between
    them is by repair specificity (truncated is narrower, runs
    first). Stringified takes the non-empty branch."""
    return [TruncatedJsonHandler(), OverEscapedKeysHandler(),
            StringifiedArgsHandler()]
