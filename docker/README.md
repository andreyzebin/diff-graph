# Docker

All-in-one container: webhook router (port 8000) + trace viewer (port 8080) + DiffGraph agents.

## Quick start

```bash
# Build from repo root
docker build -f docker/Dockerfile -t diffgraph .

# Run with your .env and config
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/config.local.yaml:/app/config.local.yaml:ro \
  -v diffgraph-data:/data \
  diffgraph
```

That's it. `.env` provides Bitbucket token and API keys, `config.local.yaml` provides LLM settings. Traces persist in the `diffgraph-data` volume.

## With .env and config.local.yaml (recommended)

Mount your existing files — same ones you use with `.venv`:

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/config.local.yaml:/app/config.local.yaml:ro \
  -v diffgraph-data:/data \
  diffgraph
```

If `.env` is mounted, it's sourced at startup. All env vars from it are available to agents.
If `config.local.yaml` is mounted, it's used as-is. Otherwise generated from env vars.

## SSL certificates

### Auto-detect from host (recommended for corporate)

Mount the host's cert directory — entrypoint auto-detects CA bundles:

```bash
# Find your host's CA path:
python3 -c "import ssl; print(ssl.get_default_verify_paths())"
# → DefaultVerifyPaths(cafile='/etc/pki/tls/certs/ca-bundle.crt', capath='/etc/pki/tls/certs', ...)

# Mount the directory containing the CA bundle:
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -v /etc/pki/ca-trust/extracted/pem:/host-certs:ro \
  -v $(pwd)/.env:/app/.env:ro \
  -v diffgraph-data:/data \
  diffgraph
```

The entrypoint scans `/host-certs` for `*.crt` and `*.pem` files and adds them to the container's trust store. Works with RHEL, CentOS, Ubuntu, Debian.

Common host paths:

| OS | Mount this |
|---|---|
| RHEL/CentOS | `-v /etc/pki/ca-trust/extracted/pem:/host-certs:ro` |
| Ubuntu/Debian | `-v /etc/ssl/certs:/host-certs:ro` |
| macOS | `-v /etc/ssl:/host-certs:ro` |

### Explicit cert files

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -v /path/to/ca-bundle.pem:/app/certs/ca.pem:ro \
  -e REQUESTS_CA_BUNDLE=/app/certs/ca.pem \
  -v $(pwd)/.env:/app/.env:ro \
  diffgraph
```

### Certs as base64 (no volume mount)

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -e BB_CA_BUNDLE_B64=$(base64 -w0 /path/to/ca-bundle.pem) \
  -v $(pwd)/.env:/app/.env:ro \
  diffgraph
```

## Corporate PyPI mirror

If pip can't reach pypi.org during build:

```bash
docker build -f docker/Dockerfile -t diffgraph \
  --build-arg DOCKER_REGISTRY=osc.corp.com/docker.io/ \
  --build-arg PYPI_MIRROR_URL=https://mirror.corp.com/repo/pypi/simple \
  --build-arg PYPI_MIRROR_TOKEN=your-token \
  .
```

| Build arg | Default | Description |
|---|---|---|
| `DOCKER_REGISTRY` | _(empty — Docker Hub)_ | Corporate Docker registry prefix (e.g. `osc.corp.com/docker.io/`) |
| `PYPI_MIRROR_URL` | _(empty — pypi.org)_ | Corporate PyPI mirror URL |
| `PYPI_MIRROR_TOKEN` | _(empty)_ | Auth token for PyPI mirror |

`DOCKER_REGISTRY` is prepended to `python:3.12-slim` in FROM. Must end with `/` if set. `trusted-host` extracted automatically from `PYPI_MIRROR_URL`.

## Data persistence

Traces DB is stored at `/data/traces.db` inside the container. Mount a volume to persist across restarts:

```bash
# Named volume (recommended)
-v diffgraph-data:/data

# Host directory
-v /path/on/host/diffgraph-data:/data
```

The directory is created automatically.

### Clear trace data

```bash
# Remove named volume
docker volume rm diffgraph-data

# Or clear just the DB file
docker exec diffgraph rm /data/traces.db
```

## Custom webhook config

Mount your own TOML to override default routing:

```bash
docker run -d --name diffgraph -p 8000:8000 -p 8080:8080 \
  -v $(pwd)/webhook.toml:/app/webhook.toml:ro \
  -v $(pwd)/.env:/app/.env:ro \
  -v diffgraph-data:/data \
  diffgraph
```

## Environment variables

All optional if `.env` and `config.local.yaml` are mounted.

| Variable | Description |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible API endpoint |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Model name (default: `deepseek-chat`) |
| `LLM_TOOL_CHOICE` | `required` (default) or `auto` |
| `LLM_TIMEOUT` | LLM request timeout in seconds (default: 600) |
| `BB_TOKEN` | Bitbucket Bearer token (alias for `BITBUCKET_SERVER_BEARER_TOKEN`) |
| `BOT_USER` | Bitbucket slug — own comments marked `[SELF]` |
| `REQUESTS_CA_BUNDLE` | Explicit CA bundle path inside container |
| `BB_CA_BUNDLE_B64` | CA bundle as base64 (auto-written to `/app/certs/`) |
| `BB_CLIENT_CERT_B64` | Client cert as base64 |
| `DATA_DIR` | Data directory (default: `/data`) |
| `FORWARD_WEBHOOK_URL` | Forward webhook URL (adds a forward agent to default config) |
| `WEBHOOK_CONFIG` | Path to webhook TOML (default: `/app/webhook.toml`) |
| `WEBHOOK_PORT` | Webhook port (default: 8000) |
| `TRACE_PORT` | Trace viewer port (default: 8080) |
| `TRACE_BASE_PATH` | URL prefix for trace viewer behind reverse proxy |

## Ports

| Port | Service |
|---|---|
| 8000 | Webhook router — configure as Bitbucket webhook URL |
| 8080 | Trace viewer — browse at http://localhost:8080 |

## Verify

```bash
# Health
curl http://localhost:8000/health

# Routes
curl http://localhost:8000/routes

# Traces
open http://localhost:8080
```

## Logs

```bash
docker logs diffgraph
docker logs diffgraph --tail 50 -f
```

## Stop

```bash
docker rm -f diffgraph
```
