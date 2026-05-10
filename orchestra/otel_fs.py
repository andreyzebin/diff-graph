"""OTel filesystem mirror — full span tree on disk.

Pairs with `orchestra/otel.py`'s SQLiteSpanExporter. Where the
SQLite exporter persists span metadata for SQL search, this one
persists the *full* request/response payloads for offline drill-
down (the AI-debugger reads them via paths recorded in span
attributes).

On-disk layout under <root>:

    spans.jsonl
        one JSON line per span (kind, name, ids, status, attrs)
    payloads/
        <span_id>.req.json    LLM request body / tool args
        <span_id>.res.json    LLM response body / tool result

Payloads can be much larger than OTel attribute size limits
(LLM messages routinely hit 100KB+), so we don't put them in
span attributes — we stash them in a process-local map keyed
by span_id and let the exporter persist on span end.

Entry points for instrumented code:

    stash_request(payload)   # call inside `with tracer.start_as_current_span(...)`
    stash_response(payload)  # …same span, after the call returns

For LLM the `payload` is `{"model", "messages", "tools", **params}`
for the request and `{"content", "tool_calls", "usage"}` for the
response. For tool calls it's `{"name", "args"}` and the raw
tool-result string respectively.

The exporter is added to the global TracerProvider only when the
caller passes `fs_root=...` to `setup_tracing()`.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


# ── Process-local payload stash ─────────────────────────────────────────────
# Mapping {span_id_hex (16 chars): {"req": payload | None, "res": payload | None}}
_PAYLOADS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _current_span_id_hex() -> Optional[str]:
    span = trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx or not ctx.span_id:
        return None
    return f"{ctx.span_id:016x}"


def stash_request(payload: Any) -> None:
    """Attach a request payload to the active span. Persisted to FS
    when the span ends. No-op outside a span."""
    sid = _current_span_id_hex()
    if not sid:
        return
    with _LOCK:
        _PAYLOADS.setdefault(sid, {})["req"] = payload


def stash_response(payload: Any) -> None:
    """Attach a response payload to the active span. No-op outside a span."""
    sid = _current_span_id_hex()
    if not sid:
        return
    with _LOCK:
        _PAYLOADS.setdefault(sid, {})["res"] = payload


# ── Filesystem exporter ────────────────────────────────────────────────────

class FSSpanExporter(SpanExporter):
    """Persist span metadata + payloads to a directory tree.

    Each call to `export()` appends one line per span to
    spans.jsonl and writes any stashed payload as
    payloads/<span_id>.{req,res}.json. Atomic-ish (each write is
    a single fwrite); if the process dies mid-call only the
    in-flight span is lost. The append-only log + content-
    addressed payloads are easy to recover/reindex.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "payloads").mkdir(exist_ok=True)
        self._spans_path = self.root / "spans.jsonl"
        self._lock = threading.Lock()

    def _write_payload(self, span_id_hex: str, kind: str, payload: Any) -> str:
        """Returns the relative path of the written file (for span attrs)."""
        rel = f"payloads/{span_id_hex}.{kind}.json"
        path = self.root / rel
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, default=str, indent=2),
                encoding="utf-8",
            )
        except Exception:
            return ""
        return rel

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with self._lock:
                with open(self._spans_path, "a", encoding="utf-8") as f:
                    for span in spans:
                        ctx = span.get_span_context()
                        sid_hex = f"{ctx.span_id:016x}"
                        # Pop and persist payloads stashed during the
                        # span's lifetime.
                        with _LOCK:
                            payloads = _PAYLOADS.pop(sid_hex, None)
                        rec_attrs = dict(span.attributes or {})
                        if payloads:
                            if "req" in payloads:
                                rel = self._write_payload(sid_hex, "req", payloads["req"])
                                if rel:
                                    rec_attrs["diffgraph.payload.req"] = rel
                            if "res" in payloads:
                                rel = self._write_payload(sid_hex, "res", payloads["res"])
                                if rel:
                                    rec_attrs["diffgraph.payload.res"] = rel
                        rec = {
                            "trace_id": f"{ctx.trace_id:032x}",
                            "span_id": sid_hex,
                            "parent_span_id": (f"{span.parent.span_id:016x}"
                                                if span.parent else None),
                            "name": span.name,
                            "kind": int(span.kind.value) if span.kind else None,
                            "start_ns": int(span.start_time or 0),
                            "end_ns": int(span.end_time or 0) if span.end_time else None,
                            "status": (span.status.status_code.name
                                       if span.status else None),
                            "attributes": rec_attrs,
                            "events": [{
                                "name": e.name,
                                "ts": e.timestamp,
                                "attributes": dict(e.attributes or {}),
                            } for e in (span.events or [])],
                        }
                        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
