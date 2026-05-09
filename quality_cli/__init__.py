"""quality-cli — search, drill, aggregate over the trace DB.

Two output modes (TODO §5e.13):

- **default (human)** — rich table, color, suggestions when a query
  returns nothing.
- **`--json`** — stable structured envelope `{"data": …, "meta": …}`
  with no color/spinners. Designed for an LLM debugger sub-agent to
  call this binary as a subprocess and parse stdout via standard
  `read_file`-style tooling.

The CLI talks to SQLiteTraceStore directly (offline). A future
`--server URL` flag will swap the storage adapter for an HTTP
client without changing command signatures.

Entry points:
    python -m quality_cli runs list ...
    python -m quality_cli runs get <id>
    python -m quality_cli tool-calls ...
    python -m quality_cli aggregates by-{provider,scenario}
"""
