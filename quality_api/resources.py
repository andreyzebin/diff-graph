"""Generic resource URI registry.

A resource is anything a task can reference: a scenario, a provider,
a lineage, a mutation, a plan, a worker, a session. The pattern
matters more than the specific types: any system entity gets a
canonical URI of the form `<kind>://<id>`, with a single source of
truth (this module) for:

- the human display name
- the UI deep-link to inspect it
- the CLI command sketch to operate on it

API responses embed the URI alongside any free-form fields, so
clients (UI + CLI) can build navigation without per-kind glue
code. CLI's `--open-in-ui` flag uses `ui_url` to launch a browser.

Adding a new kind = one entry in `_HANDLERS`. No SQL changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ResourceInfo:
    uri: str            # e.g. "scenario://REV-U-001"
    kind: str           # "scenario"
    id: str             # "REV-U-001"
    display: str        # human-readable label
    ui_path: str        # path on this server, e.g. "/qa/scenarios?id=REV-U-001"


# kind → (display_fn, ui_path_fn)
_HANDLERS: dict[str, tuple[Callable[[str], str], Callable[[str], str]]] = {
    "scenario": (
        lambda i: i,
        lambda i: f"/qa/scenarios?id={i}",
    ),
    "provider": (
        lambda i: i,
        lambda i: f"/qa/workers?provider={i}",
    ),
    "lineage": (
        lambda i: i,
        lambda i: f"/qa/traces?lineage={i}",
    ),
    "mutation": (
        lambda i: i[:8],
        lambda i: f"/qa/traces?mutation={i[:8]}",
    ),
    "plan": (
        lambda i: f"#{i}",
        lambda i: f"/qa/plans?id={i}",
    ),
    "task": (
        lambda i: f"#{i}",
        lambda i: f"/qa/queue?id={i}",
    ),
    "worker": (
        lambda i: i,
        lambda i: f"/qa/workers?id={i}",
    ),
    "session": (
        lambda i: i,
        lambda i: f"/qa/traces?session={i}",
    ),
    "trace": (
        lambda i: i,
        lambda i: f"/qa/traces?trace_id={i}",
    ),
}


def parse_uri(uri: str) -> Optional[tuple[str, str]]:
    """`scenario://X` → ('scenario', 'X'). None if malformed."""
    if not isinstance(uri, str) or "://" not in uri:
        return None
    kind, rest = uri.split("://", 1)
    kind = kind.strip().lower()
    rest = rest.strip()
    if not kind or not rest:
        return None
    return kind, rest


def make_uri(kind: str, id_: str) -> str:
    return f"{kind}://{id_}"


def resolve(uri: str) -> Optional[ResourceInfo]:
    """Look up a URI in the registry. Unknown kinds return a
    plain-text fallback so unrecognised URIs are still navigable
    (just no UI deep-link)."""
    parsed = parse_uri(uri)
    if parsed is None:
        return None
    kind, id_ = parsed
    handler = _HANDLERS.get(kind)
    if handler is None:
        return ResourceInfo(uri=uri, kind=kind, id=id_, display=id_, ui_path="")
    display_fn, ui_fn = handler
    return ResourceInfo(
        uri=uri, kind=kind, id=id_,
        display=display_fn(id_), ui_path=ui_fn(id_),
    )


def ui_url(uri: str, base: Optional[str] = None) -> Optional[str]:
    """Resolve a URI to a full URL (server origin + ui_path). Used
    by CLI's --open-in-ui flag and by API serialisations that want
    a click-through link."""
    info = resolve(uri)
    if info is None or not info.ui_path:
        return None
    origin = (base or os.environ.get("QA_SERVER_URL")
              or "http://localhost:8765").rstrip("/")
    return origin + info.ui_path


def known_kinds() -> list[str]:
    return sorted(_HANDLERS.keys())


def denormalise(uris: list[str]) -> dict:
    """Project a list of resource URIs onto the legacy QA columns
    (scenario_id / provider / lineage / mutation_hash) so generic-
    enqueue tasks still join scoring / aggregates / UI filters that
    haven't migrated to URI lookups yet. Unknown kinds are ignored.

    Last-write-wins per kind. Caller decides whether to use this
    purely as a cache (preserving caller-supplied column values) or
    let it overwrite them.
    """
    cols: dict[str, str] = {}
    kind_to_col = {
        "scenario": "scenario_id",
        "provider": "provider",
        "lineage":  "lineage",
        "mutation": "mutation_hash",
    }
    for uri in uris or []:
        parsed = parse_uri(uri)
        if parsed is None:
            continue
        kind, id_ = parsed
        col = kind_to_col.get(kind)
        if col:
            cols[col] = id_
    return cols
