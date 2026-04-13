#!/bin/bash
set -e

# ── Write certs from base64 env vars (if not already set as paths) ────
if [[ -n "$BB_CA_BUNDLE_B64" && -z "$REQUESTS_CA_BUNDLE" ]]; then
    echo "$BB_CA_BUNDLE_B64" | base64 -d > /app/certs/ca.pem
    export REQUESTS_CA_BUNDLE=/app/certs/ca.pem
fi

if [[ -n "$BB_CLIENT_CERT_B64" && -z "$BITBUCKET_SERVER_CLIENT_CERT" ]]; then
    echo "$BB_CLIENT_CERT_B64" | base64 -d > /app/certs/client.pem
    export BITBUCKET_SERVER_CLIENT_CERT=/app/certs/client.pem
fi

# ── Generate config.local.yaml from env vars ────────────
cat > /app/config.local.yaml <<EOF
llm:
  api_url: "${LLM_BASE_URL:-}"
  api_key: "${LLM_API_KEY:-}"
  model: "${LLM_MODEL:-deepseek-chat}"
EOF

# ── Generate webhook.toml if not mounted ─────────────────
if [[ ! -f "$WEBHOOK_CONFIG" ]]; then
    cat > "$WEBHOOK_CONFIG" <<TOML
[server]
port = ${WEBHOOK_PORT:-8000}

[agents.diffgraph]
trigger = "cli"
command = 'cd /app && python cli.py run --pr-url="{pr_url}" --post-comments'
timeout = 600

[events]
"pr:opened" = ["review"]
"pr:comment:added" = "parse"
"repo:refs_changed" = []

[[routes]]
name = "default"
when = "true"
agent = "diffgraph"
TOML

    # Add forward agent if URL provided
    if [[ -n "$FORWARD_WEBHOOK_URL" ]]; then
        cat >> "$WEBHOOK_CONFIG" <<TOML

[agents.forward]
trigger = "webhook"
base_url = "${FORWARD_WEBHOOK_URL}"
timeout = 300
TOML
    fi
fi

# ── Export Bitbucket vars for diffgraph ──────────────────
export BITBUCKET_SERVER_BEARER_TOKEN="${BB_TOKEN:-${BITBUCKET_SERVER_BEARER_TOKEN:-}}"

# ── Start trace server in background ─────────────────────
echo "Starting trace server on port ${TRACE_PORT:-8080}..."
python -c "
import uvicorn
from tracing.server.app import app
uvicorn.run(app, host='0.0.0.0', port=${TRACE_PORT:-8080}, log_level='warning')
" &

# ── Start webhook server (foreground) ────────────────────
echo "Starting webhook router on port ${WEBHOOK_PORT:-8000}..."
exec python -m webhook --config "$WEBHOOK_CONFIG" --port "${WEBHOOK_PORT:-8000}" --host 0.0.0.0
