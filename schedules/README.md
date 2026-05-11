# Schedules

Yaml-driven auto-plan configs. Server walks this directory (recursively,
skipping `drafts/`) on startup and via `POST /api/qa/auto-plan/reload-yaml`,
upserting each definition into `qa_auto_plan_configs`. **Yaml wins** — any
UI/API edits to a yaml-imported row are clobbered on next reload.

Override the directory with `SCHEDULES_DIR=/path/to/schedules` (env).

Each yaml is either one schedule (top-level keys) or a `schedules:` array.
`${ENV}` placeholders in path-like fields are expanded from process env.

See `unit-tier.yaml` for an example.
