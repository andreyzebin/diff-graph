"""
File-level extraction cache for DiffGraph.

Cache key: SHA256(file_content) + model slug
Location:  ~/.cache/diffgraph/extractions/<model_slug>/<sha256>.json
           (override via DIFFGRAPH_CACHE_DIR env var or config)

Only pure extraction data is cached — runtime fields (is_changed,
full_code, is_expanded) are never written and are left at defaults
when a Module is loaded from cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from .model import Dependency, Module, Symbol

log = logging.getLogger(__name__)


def _cache_root() -> Path:
    base = os.environ.get("DIFFGRAPH_CACHE_DIR") or (
        Path.home() / ".cache" / "diffgraph"
    )
    return Path(base)


def _model_slug(model: str) -> str:
    """Sanitize model name for use as a directory component."""
    return re.sub(r"[^\w\-.]", "_", model)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _cache_path(content: str, model: str) -> Path:
    root = _cache_root() / "extractions" / _model_slug(model)
    return root / f"{_sha256(content)}.json"


# ── serialization ─────────────────────────────────────────────────────────────

def _module_to_dict(mod: Module) -> dict:
    return {
        "id": mod.id,
        "name": mod.name,
        "lang": mod.lang,
        "summary": mod.summary,
        "symbols": [
            {
                "name": s.name,
                "kind": s.kind,
                "signature": s.signature,
                "summary": s.summary,
                "annotations": s.annotations,
                "start_line": s.start_line,
                "end_line": s.end_line,
                # is_changed, is_expanded, full_code are runtime — not cached
            }
            for s in mod.symbols
        ],
        "dependencies": [
            {
                "name": d.name,
                "fqn": d.fqn,
                "usage": d.usage,
                # file_path resolved at runtime — not cached
            }
            for d in mod.dependencies
        ],
    }


def _module_from_dict(data: dict) -> Module:
    return Module(
        id=data["id"],
        name=data["name"],
        lang=data["lang"],
        summary=data.get("summary", ""),
        symbols=[
            Symbol(
                name=s["name"],
                kind=s.get("kind", ""),
                signature=s.get("signature", s["name"]),
                summary=s.get("summary", ""),
                annotations=s.get("annotations", []),
                start_line=int(s.get("start_line", 1)),
                end_line=int(s.get("end_line", 1)),
            )
            for s in data.get("symbols", [])
        ],
        dependencies=[
            Dependency(
                name=d["name"],
                fqn=d.get("fqn", d["name"]),
                usage=d.get("usage", ""),
            )
            for d in data.get("dependencies", [])
        ],
    )


# ── public API ────────────────────────────────────────────────────────────────

def load(content: str, model: str) -> Optional[Module]:
    """Return a cached Module for this file content + model, or None."""
    path = _cache_path(content, model)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mod = _module_from_dict(data)
        log.debug("cache hit: %s", path.name)
        return mod
    except Exception as exc:
        log.warning("cache read error %s: %s", path, exc)
        return None


def save(content: str, model: str, module: Module) -> None:
    """Atomically write a Module to the cache."""
    path = _cache_path(content, model)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_module_to_dict(module), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic on same filesystem
        log.debug("cache saved: %s", path.name)
    except Exception as exc:
        log.warning("cache write error %s: %s", path, exc)
