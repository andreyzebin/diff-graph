#!/bin/bash
set -e

# ── SSL certs: auto-detect from host mount or explicit path ──────────
# Priority: explicit REQUESTS_CA_BUNDLE > base64 env > host mount > default
if [[ -z "$REQUESTS_CA_BUNDLE" && -n "$BB_CA_BUNDLE_B64" ]]; then
    echo "$BB_CA_BUNDLE_B64" | base64 -d > /app/certs/ca.pem
    export REQUESTS_CA_BUNDLE=/app/certs/ca.pem
    echo "CA bundle: written from BB_CA_BUNDLE_B64"
fi

if [[ -z "$REQUESTS_CA_BUNDLE" && -d "/host-certs" ]]; then
    # Auto-detect host CA bundle from mounted /host-certs
    for f in /host-certs/tls-ca-bundle.pem \
             /host-certs/ca-bundle.crt \
             /host-certs/ca-certificates.crt; do
        if [ -f "$f" ]; then
            cat "$f" >> /etc/ssl/certs/ca-certificates.crt
            echo "CA bundle: loaded host CAs from $f"
            break
        fi
    done
    # Also scan for individual .crt/.pem files
    for f in /host-certs/*.crt /host-certs/*.pem; do
        [ -f "$f" ] || continue
        cat "$f" >> /etc/ssl/certs/ca-certificates.crt
        echo "CA bundle: added $f"
    done
    update-ca-certificates 2>/dev/null || true
fi

if [[ -n "$REQUESTS_CA_BUNDLE" ]]; then
    # Make sure Python/curl/git all use it
    export SSL_CERT_FILE="${REQUESTS_CA_BUNDLE}"
    export CURL_CA_BUNDLE="${REQUESTS_CA_BUNDLE}"
    echo "CA bundle: $REQUESTS_CA_BUNDLE"
fi

if [[ -n "$BB_CLIENT_CERT_B64" && -z "$BITBUCKET_SERVER_CLIENT_CERT" ]]; then
    echo "$BB_CLIENT_CERT_B64" | base64 -d > /app/certs/client.pem
    export BITBUCKET_SERVER_CLIENT_CERT=/app/certs/client.pem
fi

# ── Config: use mounted files or generate from env ───────────────────
# .env: sourced if mounted
if [ -f /app/.env ]; then
    set -a; source /app/.env; set +a
    echo "Config: loaded .env"
fi

# config.local.yaml: use mounted or generate from env
if [ ! -f /app/config.local.yaml ]; then
    cat > /app/config.local.yaml <<EOF
llm:
  api_url: "${LLM_BASE_URL:-}"
  api_key: "${LLM_API_KEY:-}"
  model: "${LLM_MODEL:-deepseek-chat}"
  tool_choice: "${LLM_TOOL_CHOICE:-required}"
  timeout: ${LLM_TIMEOUT:-600}

review:
  bot_user: "${BOT_USER:-}"
EOF
    echo "Config: generated config.local.yaml from env"
else
    echo "Config: using mounted config.local.yaml"
fi

# ── Data dir: traces DB ──────────────────────────────────────────────
DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
# Point trace DB to data dir (persisted via volume)
export DIFFGRAPH_TRACES_DB="${DATA_DIR}/traces.db"

# ── Bitbucket vars ───────────────────────────────────────────────────
export BITBUCKET_SERVER_BEARER_TOKEN="${BB_TOKEN:-${BITBUCKET_SERVER_BEARER_TOKEN:-}}"

# ── Generate webhook.toml if not mounted ─────────────────────────────
if [[ ! -f "$WEBHOOK_CONFIG" ]]; then
    cat > "$WEBHOOK_CONFIG" <<TOML
[server]
port = ${WEBHOOK_PORT:-8000}

[agents.dg]
trigger = "cli"
command = 'cd /app && python cli.py run --pr-url="{pr_url}" --message="{message}" --comment-id={comment_id}'
timeout = 600

[agents.dg-review]
trigger = "cli"
command = 'cd /app && python cli.py run --pr-url="{pr_url}" --agent=reviewer'
timeout = 600

[events]
"pr:opened" = ["review"]
"pr:comment:added" = "parse"
"pr:from_ref_updated" = ["review"]
"repo:refs_changed" = []

[[routes]]
name = "default"
when = "true"
agent = "dg"
review = "dg-review"
TOML

    if [[ -n "$FORWARD_WEBHOOK_URL" ]]; then
        cat >> "$WEBHOOK_CONFIG" <<TOML

[agents.forward]
trigger = "webhook"
base_url = "${FORWARD_WEBHOOK_URL}"
timeout = 300
TOML
    fi
    echo "Config: generated webhook.toml"
else
    echo "Config: using mounted webhook.toml"
fi

# ── Start trace server in background ─────────────────────────────────
echo "Starting trace server on port ${TRACE_PORT:-8080}..."
python -c "
import uvicorn
from tracing.server.app import app
uvicorn.run(app, host='0.0.0.0', port=${TRACE_PORT:-8080}, log_level='warning')
" &

# ── Start webhook server (foreground) ────────────────────────────────
echo "Starting webhook router on port ${WEBHOOK_PORT:-8000}..."
exec python -m webhook --config "$WEBHOOK_CONFIG" --port "${WEBHOOK_PORT:-8000}" --host 0.0.0.0
