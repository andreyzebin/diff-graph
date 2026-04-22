#!/bin/bash
set -e

DEBUG=${DEBUG:-0}
dbg() { [[ "$DEBUG" == "1" ]] && echo "[debug] $*" || true; }

[[ "$DEBUG" == "1" ]] && echo "=== DiffGraph entrypoint (DEBUG mode) ==="

# ── SSL certs: auto-detect from host mount or explicit path ──────────
# Priority: explicit REQUESTS_CA_BUNDLE > base64 env > host mount > default

dbg "Build-time certs in container:"
dbg "$(ls -la /usr/local/share/ca-certificates/corp/ 2>/dev/null || echo '  (none)')"
dbg "System CA bundle: $(wc -c < /etc/ssl/certs/ca-certificates.crt 2>/dev/null || echo 'missing') bytes"

if [[ -z "$REQUESTS_CA_BUNDLE" && -n "$BB_CA_BUNDLE_B64" ]]; then
    echo "$BB_CA_BUNDLE_B64" | base64 -d > /app/certs/ca.pem
    export REQUESTS_CA_BUNDLE=/app/certs/ca.pem
    echo "CA bundle: written from BB_CA_BUNDLE_B64"
fi

if [[ -z "$REQUESTS_CA_BUNDLE" && -d "/host-certs" ]]; then
    dbg "/host-certs mounted, scanning..."
    for f in /host-certs/tls-ca-bundle.pem \
             /host-certs/ca-bundle.crt \
             /host-certs/ca-certificates.crt; do
        if [ -f "$f" ]; then
            cat "$f" >> /etc/ssl/certs/ca-certificates.crt
            echo "CA bundle: loaded host CAs from $f"
            break
        fi
    done
    for f in /host-certs/*.crt /host-certs/*.pem; do
        [ -f "$f" ] || continue
        cat "$f" >> /etc/ssl/certs/ca-certificates.crt
        dbg "  added $f"
    done
    update-ca-certificates 2>/dev/null || true
else
    dbg "/host-certs not mounted"
fi

# ── Config: use mounted files or generate from env ───────────────────
# .env: sourced if mounted (BEFORE checking REQUESTS_CA_BUNDLE path)
if [ -f /app/.env ]; then
    set -a; source /app/.env; set +a
    echo "Config: loaded .env"
    dbg "  REQUESTS_CA_BUNDLE=$REQUESTS_CA_BUNDLE"
    dbg "  BITBUCKET_SERVER_CLIENT_CERT=${BITBUCKET_SERVER_CLIENT_CERT:-}"
    dbg "  BITBUCKET_SERVER__CLIENT_CERT=${BITBUCKET_SERVER__CLIENT_CERT:-}"
fi

# Fix CA bundle path from .env that doesn't exist in container
if [[ -n "$REQUESTS_CA_BUNDLE" && ! -f "$REQUESTS_CA_BUNDLE" ]]; then
    dbg "REQUESTS_CA_BUNDLE=$REQUESTS_CA_BUNDLE not found in container"
    echo "CA bundle: $REQUESTS_CA_BUNDLE not found, using /etc/ssl/certs/ca-certificates.crt"
    export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
fi

# Default to the system bundle that update-ca-certificates writes to.
# Python on python:3.13-slim reads /usr/lib/ssl/cert.pem by default, which
# does NOT contain corp CAs added at build time.
if [[ -z "$REQUESTS_CA_BUNDLE" && -f /etc/ssl/certs/ca-certificates.crt ]]; then
    export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    dbg "REQUESTS_CA_BUNDLE defaulted to /etc/ssl/certs/ca-certificates.crt"
fi

if [[ -n "$REQUESTS_CA_BUNDLE" ]]; then
    export SSL_CERT_FILE="${REQUESTS_CA_BUNDLE}"
    export CURL_CA_BUNDLE="${REQUESTS_CA_BUNDLE}"
    echo "CA bundle: $REQUESTS_CA_BUNDLE"
    dbg "  size: $(wc -c < "$REQUESTS_CA_BUNDLE") bytes"
fi

if [[ -n "$BB_CLIENT_CERT_B64" && -z "$BITBUCKET_SERVER_CLIENT_CERT" ]]; then
    echo "$BB_CLIENT_CERT_B64" | base64 -d > /app/certs/client.pem
    export BITBUCKET_SERVER_CLIENT_CERT=/app/certs/client.pem
fi

# Fix client cert paths from .env that don't exist in container
for var in BITBUCKET_SERVER_CLIENT_CERT BITBUCKET_SERVER__CLIENT_CERT; do
    val="${!var}"
    if [[ -n "$val" && ! -f "$val" ]]; then
        dbg "$var=$val not found in container"
        echo "Warning: $var=$val not found in container, unsetting"
        unset "$var"
    fi
done

# Debug: show final SSL state
if [[ "$DEBUG" == "1" ]]; then
    echo "[debug] === SSL summary ==="
    echo "[debug]   REQUESTS_CA_BUNDLE=${REQUESTS_CA_BUNDLE:-<unset>}"
    echo "[debug]   SSL_CERT_FILE=${SSL_CERT_FILE:-<unset>}"
    echo "[debug]   BITBUCKET_SERVER_CLIENT_CERT=${BITBUCKET_SERVER_CLIENT_CERT:-<unset>}"
    echo "[debug]   BITBUCKET_SERVER__CLIENT_CERT=${BITBUCKET_SERVER__CLIENT_CERT:-<unset>}"
    echo "[debug]   Python SSL paths:"
    python -c "import ssl; p=ssl.get_default_verify_paths(); print(f'    cafile={p.cafile}'); print(f'    capath={p.capath}')" 2>/dev/null || true
    echo "[debug]   truststore available: $(python -c 'import truststore; print("yes")' 2>/dev/null || echo 'no')"
    echo "[debug] ====================="
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
export DIFFGRAPH_TRACES_DB="${DATA_DIR}/traces.db"
dbg "Traces DB: $DIFFGRAPH_TRACES_DB"

# ── Bitbucket vars ───────────────────────────────────────────────────
export BITBUCKET_SERVER_BEARER_TOKEN="${BB_TOKEN:-${BITBUCKET_SERVER_BEARER_TOKEN:-}}"
dbg "BB token: ${BITBUCKET_SERVER_BEARER_TOKEN:+set (${#BITBUCKET_SERVER_BEARER_TOKEN} chars)}"

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

dbg "webhook.toml contents:"
[[ "$DEBUG" == "1" ]] && cat "$WEBHOOK_CONFIG"

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
