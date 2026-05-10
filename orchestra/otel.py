"""OpenTelemetry integration — Phase 1.

Wraps writers with the OTel Tracer API; each `with tracer.start_as_current_span(...)`
emits a Span when the block exits. Spans are written to a new
`otel_spans` table in our existing `~/.diffgraph/traces.db` via a
custom `SpanExporter`. No collector daemon needed; same DB our
existing UI/CLI/API server already reads.

Inter-process propagation: the W3C TraceContext header
`traceparent` is exchanged via the `TRACEPARENT` env var when one
process spawns another (DiscoverySupervisor → WorkerSupervisor →
worker subprocess → bench → diff-graph CLI). At process start
`setup_tracing()` reads the env, hydrates the parent context, and
all subsequent spans inherit `trace_id`.

Schema (idempotent — created on first export):

    otel_spans (
      trace_id        TEXT NOT NULL,         -- 32-hex
      span_id         TEXT PRIMARY KEY,      -- 16-hex
      parent_span_id  TEXT,                  -- 16-hex or NULL for root
      name            TEXT NOT NULL,
      kind            INTEGER,               -- OTel SpanKind
      service_name    TEXT,                  -- resource.service.name
      start_ns        INTEGER NOT NULL,
      end_ns          INTEGER,
      status_code     TEXT,                  -- OK | ERROR | UNSET
      status_message  TEXT,
      attributes      TEXT,                  -- JSON
      events          TEXT,                  -- JSON array (per-span events)
      links           TEXT                   -- JSON array
    );
    CREATE INDEX idx_otel_spans_trace  ON otel_spans(trace_id);
    CREATE INDEX idx_otel_spans_parent ON otel_spans(parent_span_id);
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor, SpanExporter, SpanExportResult,
)
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)


_DEFAULT_DB = Path.home() / ".diffgraph" / "traces.db"
_TRACER_PROVIDER: Optional[TracerProvider] = None
_LOCK = threading.Lock()


# ── Custom SQLite exporter ──────────────────────────────────────────────────

class SQLiteSpanExporter(SpanExporter):
    """Persist spans to ~/.diffgraph/traces.db's `otel_spans` table.

    Idempotent on schema (re-creates indexes/table if missing). Handles
    the cross-process case where multiple writers share the file by
    using a short busy_timeout — collisions are rare since each span is
    one INSERT.
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    def _ensure_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS otel_spans (
                  trace_id        TEXT NOT NULL,
                  span_id         TEXT PRIMARY KEY,
                  parent_span_id  TEXT,
                  name            TEXT NOT NULL,
                  kind            INTEGER,
                  service_name    TEXT,
                  start_ns        INTEGER NOT NULL,
                  end_ns          INTEGER,
                  status_code     TEXT,
                  status_message  TEXT,
                  attributes      TEXT,
                  events          TEXT,
                  links           TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_otel_spans_trace
                  ON otel_spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_otel_spans_parent
                  ON otel_spans(parent_span_id);
                -- Global time-funnel index. Every read query
                -- starts with `WHERE start_ns >= ?` (Jaeger
                -- convention) so the planner can clip the table
                -- to a small recent slice before any json_extract.
                CREATE INDEX IF NOT EXISTS idx_otel_spans_start
                  ON otel_spans(start_ns DESC);
                -- Filter on agent.* / llm.request / tool.* common.
                CREATE INDEX IF NOT EXISTS idx_otel_spans_name_start
                  ON otel_spans(name, start_ns DESC);
            """)
            c.commit()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with self._lock, self._conn() as c:
                for span in spans:
                    ctx = span.get_span_context()
                    parent_ctx = span.parent
                    service_name = ""
                    try:
                        service_name = (span.resource.attributes or {}).get(
                            "service.name", "") if span.resource else ""
                    except Exception:
                        pass
                    c.execute(
                        """INSERT OR REPLACE INTO otel_spans
                           (trace_id, span_id, parent_span_id, name,
                            kind, service_name,
                            start_ns, end_ns,
                            status_code, status_message,
                            attributes, events, links)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"{ctx.trace_id:032x}",
                            f"{ctx.span_id:016x}",
                            (f"{parent_ctx.span_id:016x}"
                                if parent_ctx else None),
                            span.name,
                            int(span.kind.value) if span.kind else None,
                            service_name,
                            int(span.start_time or 0),
                            int(span.end_time or 0) if span.end_time else None,
                            (span.status.status_code.name
                                if span.status else None),
                            (span.status.description if span.status else None),
                            json.dumps(dict(span.attributes or {}),
                                       default=str),
                            json.dumps([{
                                "name": e.name,
                                "timestamp": e.timestamp,
                                "attributes": dict(e.attributes or {}),
                            } for e in (span.events or [])], default=str),
                            json.dumps([{
                                "trace_id": f"{l.context.trace_id:032x}",
                                "span_id": f"{l.context.span_id:016x}",
                                "attributes": dict(l.attributes or {}),
                            } for l in (span.links or [])], default=str),
                        ),
                    )
                c.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            # Tracing failures must NEVER crash the agent. Swallow.
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ── Setup / propagation ─────────────────────────────────────────────────────

def setup_tracing(service_name: str = "diffgraph",
                  db_path: str | Path = _DEFAULT_DB,
                  fs_root: Optional[str | Path] = None) -> trace.Tracer:
    """Initialise the global TracerProvider with the SQLite exporter
    (and optionally the filesystem exporter when `fs_root` is set).

    Idempotent — calling more than once in one process is a no-op
    after the first. Reads `TRACEPARENT` from env to inherit the
    upstream caller's trace context (cross-process propagation).
    """
    global _TRACER_PROVIDER
    with _LOCK:
        if _TRACER_PROVIDER is not None:
            return trace.get_tracer(service_name)
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(SQLiteSpanExporter(db_path)),
        )
        if fs_root:
            from .otel_fs import FSSpanExporter
            provider.add_span_processor(
                BatchSpanProcessor(FSSpanExporter(fs_root)),
            )
        trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider

    # Inherit upstream trace context if TRACEPARENT (W3C) is set.
    # This is what makes "supervisor → worker → bench → diff-graph"
    # show as ONE trace instead of four disjoint ones.
    traceparent = os.environ.get("TRACEPARENT", "").strip()
    if traceparent:
        carrier = {"traceparent": traceparent}
        ts = os.environ.get("TRACESTATE", "").strip()
        if ts:
            carrier["tracestate"] = ts
        ctx = TraceContextTextMapPropagator().extract(carrier)
        # Stash on the global ctxvar so child spans become children
        # of the upstream span automatically.
        from opentelemetry import context as otel_ctx
        otel_ctx.attach(ctx)

    return trace.get_tracer(service_name)


# ── Domain attributes propagation ──────────────────────────────────────────
# Phase 3: instead of synthesising domain dimensions (scenario_id,
# mutation, plan_id, …) from JOINs at read time, we stamp them on
# EVERY span at write time. cli.py / worker call set_domain_attrs
# at the entry point; Agent.run / observe() merge what's there into
# their own span attributes. Cross-process: env vars (existing
# DIFFGRAPH_*) carry the same info into a child subprocess where
# cli.py re-runs set_domain_attrs.
from contextvars import ContextVar
from typing import Any
_DOMAIN: ContextVar[dict] = ContextVar("_DIFFGRAPH_DOMAIN", default={})


def set_domain_attrs(**kwargs: Any) -> None:
    """Stamp domain dimensions onto the current OTel context.

    Subsequent spans (created via tracer.start_as_current_span /
    Agent.run / observe()) will get these attrs merged. Pass `None`
    or empty values to skip a key.
    """
    cur = _DOMAIN.get() or {}
    new = dict(cur)
    for k, v in kwargs.items():
        if v is None or v == "":
            continue
        new[f"diffgraph.{k}"] = str(v)
    _DOMAIN.set(new)


def get_domain_attrs() -> dict:
    """Snapshot the active domain attrs. Used by Agent.run to merge
    into its `agent.<name>` span."""
    return dict(_DOMAIN.get() or {})


# ── Single observable wrapper ──────────────────────────────────────────────
# One context manager used for BOTH llm calls and tool calls, so the
# trace shape is identical and each writer never has to remember the
# OTel API surface or how to record exceptions. Inside the with-block
# call stash_request(...) and stash_response(...) from otel_fs to
# attach the full payloads (LLM messages / tool args / tool results)
# — the FSSpanExporter persists them next to the span.
from contextlib import contextmanager


@contextmanager
def observe(name: str, attributes: Optional[dict] = None,
            service: str = "diffgraph"):
    """Open a span around an observable operation.

    Use for any unit of work whose payload an AI debugger may want
    to drill into — LLM calls, tool calls, etc. Records exceptions
    as ERROR status before re-raising so the trace shows red.

    Example::

        from orchestra.otel import observe
        from orchestra.otel_fs import stash_request, stash_response

        with observe("llm.request", {"llm.model": model}):
            stash_request({"messages": messages, "tools": tools})
            response = stream_llm(...)
            stash_response({"content": response.content})
    """
    from opentelemetry.trace import StatusCode, Status as _Status
    tracer = trace.get_tracer(service)
    # Merge domain attrs (scenario_id, mutation, plan_id, etc.)
    # set by cli.session / worker so EVERY span carries the
    # dimensions needed for filter+search at read time. Avoids
    # joining `runs` or aggregating `events` in /qa/traces.
    merged = {**get_domain_attrs(), **(attributes or {})}
    with tracer.start_as_current_span(name, attributes=merged) as span:
        try:
            yield span
        except Exception as exc:
            try:
                span.record_exception(exc)
                span.set_status(_Status(StatusCode.ERROR, type(exc).__name__))
            except Exception:
                pass
            raise


def current_traceparent() -> str:
    """Render the active span's context as a W3C `traceparent` string.

    Empty if no active span. Pass to a subprocess via env to chain
    tracing across the boundary:
        env["TRACEPARENT"] = current_traceparent()
    """
    carrier: dict = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent", "")
