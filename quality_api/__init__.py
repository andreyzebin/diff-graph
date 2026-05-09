"""Quality-api task queue — DB-backed worker contract.

Implements §5e.4 (worker model) and §5e.9 step 3 (`/qa/tasks/*`
lease / heartbeat / finish endpoints).

Schema lives in ~/.diffgraph/traces.db (the one orchestra already
writes to). Tables added idempotently via `ensure_schema()`.

The lease query is the only transactional piece — `SELECT … LIMIT 1
FOR UPDATE` semantic via SQLite's `BEGIN IMMEDIATE`. Workers can
crash; the reaper recovers leased-but-dead tasks back to queued.

Design notes:
- Queue is FIFO with `priority` column for sentinel ordering.
- Each task carries enough payload (scenario_id, provider,
  attempt_n, mutation_hash, branch) for the worker to invoke the
  bench without hitting the API again.
- Lease has an explicit `lease_expires_at`; heartbeat extends it.
- Reaper runs at server startup and on demand via /qa/tasks/reap.
"""
