# Docker

All-in-one container: webhook router (port 8000) + trace viewer (port 8080) + diffgraph agent.

## Build

```bash
# From repo root
docker build -f docker/Dockerfile -t diffgraph .
```

## Run

### Minimal (no Bitbucket, just webhook router)

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8081:8080 \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  -e LLM_MODEL=deepseek-chat \
  diffgraph
```

### With Bitbucket Server (token auth)

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8081:8080 \
  -e BITBUCKET_SERVER_BEARER_TOKEN=your-token \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  -e LLM_MODEL=deepseek-chat \
  diffgraph
```

### With Bitbucket Server (mTLS + CA bundle)

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8081:8080 \
  -v /path/to/certs:/certs:ro \
  -e REQUESTS_CA_BUNDLE=/certs/ca-bundle.pem \
  -e BITBUCKET_SERVER_CLIENT_CERT=/certs/client.pem \
  -e BITBUCKET_SERVER_BEARER_TOKEN=your-token \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  -e LLM_MODEL=deepseek-chat \
  -e "NO_PROXY=localhost,*.your-domain.com,api.deepseek.com" \
  diffgraph
```

### With certs as base64 (no volume mount)

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8081:8080 \
  -e BB_CA_BUNDLE_B64=$(base64 -w0 /path/to/ca-bundle.pem) \
  -e BB_CLIENT_CERT_B64=$(base64 -w0 /path/to/client.pem) \
  -e BITBUCKET_SERVER_BEARER_TOKEN=your-token \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  -e LLM_MODEL=deepseek-chat \
  diffgraph
```

### Custom webhook config

Mount your own TOML to override the default routing:

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8081:8080 \
  -v /path/to/webhook.toml:/app/webhook.toml:ro \
  -e BITBUCKET_SERVER_BEARER_TOKEN=your-token \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  diffgraph
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BITBUCKET_SERVER_BEARER_TOKEN` | For PR mode | Bitbucket personal access token |
| `REQUESTS_CA_BUNDLE` | If corporate CA | Path to CA cert bundle inside container |
| `BITBUCKET_SERVER_CLIENT_CERT` | If mTLS | Path to client PEM inside container |
| `BB_CA_BUNDLE_B64` | Alternative | CA bundle as base64 (auto-written to /app/certs/) |
| `BB_CLIENT_CERT_B64` | Alternative | Client cert as base64 (auto-written to /app/certs/) |
| `LLM_BASE_URL` | Yes | OpenAI-compatible API endpoint |
| `LLM_API_KEY` | Yes | API key |
| `LLM_MODEL` | No | Model name (default: deepseek-chat) |
| `LLM_CA_BUNDLE` | If corporate CA | Path to CA bundle for LLM endpoint TLS |
| `FORWARD_WEBHOOK_URL` | No | URL for forward webhook trigger |
| `WEBHOOK_CONFIG` | No | Path to TOML config (default: /app/webhook.toml) |
| `WEBHOOK_PORT` | No | Webhook server port (default: 8000) |
| `TRACE_PORT` | No | Trace viewer port (default: 8080) |
| `TRACE_BASE_PATH` | No | URL prefix for trace viewer behind reverse proxy (e.g. `/evo/traces-ui`) |
| `NO_PROXY` | If needed | Comma-separated no-proxy hosts |

## Ports

| Port | Service |
|---|---|
| 8000 | Webhook router — configure as Bitbucket webhook URL |
| 8080 | Trace viewer — browse run history and agent traces |

## Test with curl

### Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","agents":["diffgraph"],"routes":1}
```

### Show routes

```bash
curl http://localhost:8000/routes
```

### Webhook ping (connection test)

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"test": true}'
# {"status":"ok","message":"webhook connected"}
```

### Simulate PR opened event

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "x-bitbucket-server-url: https://bitbucket.example.com" \
  -d '{
    "eventKey": "pr:opened",
    "pullRequest": {
      "id": 42,
      "title": "Fix null check in OrderService",
      "fromRef": {
        "displayId": "feature/fix-npe",
        "repository": {"slug": "my-service", "project": {"key": "MYPROJ"}}
      },
      "toRef": {
        "displayId": "main",
        "repository": {"slug": "my-service", "project": {"key": "MYPROJ"}}
      },
      "author": {"user": {"name": "developer"}}
    }
  }'
# {"status":"accepted","mode":"commands","decisions":[{"command":"review",...}]}
```

### Simulate /ask comment

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "x-bitbucket-server-url: https://bitbucket.example.com" \
  -d '{
    "eventKey": "pr:comment:added",
    "pullRequest": {
      "id": 42,
      "title": "Fix null check",
      "fromRef": {
        "displayId": "feature/fix-npe",
        "repository": {"slug": "my-service", "project": {"key": "MYPROJ"}}
      },
      "toRef": {
        "displayId": "main",
        "repository": {"slug": "my-service", "project": {"key": "MYPROJ"}}
      },
      "author": {"user": {"name": "developer"}}
    },
    "comment": {"id": 123, "text": "@diffgraph /ask Is this null-safe?"}
  }'
# {"status":"accepted","mode":"commands","decisions":[{"command":"ask","args":"Is this null-safe?",...}]}
```

### Trace viewer

Open http://localhost:8081 in browser to see run history and agent traces.

## Behind nginx (reverse proxy)

Deploy with path-based routing for corporate environments:

```bash
docker run -d --name diffgraph \
  -e TRACE_BASE_PATH=/evo/traces-ui \
  -e BITBUCKET_SERVER_BEARER_TOKEN=your-token \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_API_KEY=sk-your-key \
  diffgraph
```

nginx config:

```nginx
# Webhook (no prefix needed — Bitbucket sends to this URL directly)
location /evo/webhook {
    proxy_pass http://diffgraph:8000/webhook;
}

# Trace viewer (with path prefix)
location /evo/traces-ui/ {
    proxy_pass http://diffgraph:8080/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Access:
- Bitbucket webhook URL: `https://your-company.com/evo/webhook`
- Trace viewer: `https://your-company.com/evo/traces-ui/`

Without `TRACE_BASE_PATH` — all paths from root (default, no proxy needed).

## Logs

```bash
docker logs diffgraph
docker logs diffgraph --tail 20 -f
```

## Stop

```bash
docker rm -f diffgraph
```
