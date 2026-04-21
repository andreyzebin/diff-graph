ARG DOCKER_REGISTRY=""
FROM ${DOCKER_REGISTRY}python:3.13-slim-bookworm

# ── Corporate CA bundle (optional, for APT/pip SSL during build) ────
# Drop .crt/.pem files into docker/certs/ before building, OR
# set BUILD_CERTS_DIR in .env to copy from a host directory.
# See build command below — it copies certs into docker/certs/ first.
COPY docker/certs/ /usr/local/share/ca-certificates/corp/
RUN update-ca-certificates 2>/dev/null || true

# ── Corporate APT mirror (optional) ─────────────────────
# APT_MIRROR_URL: base URL like http://mirror.corp.com/repo/debian/debian_yandex
# Codename auto-detected from the base image (bookworm for python:3.13-slim-bookworm)
ARG APT_MIRROR_URL=""
RUN if [ -n "$APT_MIRROR_URL" ]; then \
      CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME"); \
      rm -f /etc/apt/sources.list.d/*; \
      echo "deb [trusted=yes] ${APT_MIRROR_URL} ${CODENAME} main" > /etc/apt/sources.list; \
      echo "deb [trusted=yes] ${APT_MIRROR_URL} ${CODENAME}-updates main contrib" >> /etc/apt/sources.list; \
      echo "deb [trusted=yes] ${APT_MIRROR_URL}_security ${CODENAME}-security main contrib" >> /etc/apt/sources.list; \
    fi

# ── System deps ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# ── Corporate PyPI mirror (optional, via build arg) ──────
# PYPI_MIRROR_URL: full URL like https://mirror.corp.com/repo/pypi/simple
# PYPI_MIRROR_TOKEN: token for auth (optional)
ARG PYPI_MIRROR_URL=""
ARG PYPI_MIRROR_TOKEN=""
RUN if [ -n "$PYPI_MIRROR_URL" ]; then \
      MIRROR_HOST=$(echo "$PYPI_MIRROR_URL" | sed 's|https\?://||; s|/.*||'); \
      if [ -n "$PYPI_MIRROR_TOKEN" ]; then \
        AUTH_URL=$(echo "$PYPI_MIRROR_URL" | sed "s|://|://token:${PYPI_MIRROR_TOKEN}@|"); \
        pip config set global.index-url "$AUTH_URL"; \
      else \
        pip config set global.index-url "$PYPI_MIRROR_URL"; \
      fi; \
      pip config set global.trusted-host "$MIRROR_HOST"; \
    fi

WORKDIR /app

# ── Install deps (cached layer) ─────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy app ─────────────────────────────────────────────
COPY . .
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

RUN mkdir -p /app/certs /data

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1
ENV WEBHOOK_CONFIG=/app/webhook.toml
ENV WEBHOOK_PORT=8000
ENV TRACE_PORT=8080

EXPOSE 8000 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
