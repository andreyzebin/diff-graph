ARG DOCKER_REGISTRY=""
FROM ${DOCKER_REGISTRY}python:3.13-slim

# ── Corporate CA bundle (optional, for pip SSL during build) ─
COPY docker/certs/ /usr/local/share/ca-certificates/corp/
RUN chmod -R 644 /usr/local/share/ca-certificates/corp/* 2>/dev/null || true
RUN update-ca-certificates 2>/dev/null || true

# ── System deps ──────────────────────────────────────────────
# git is the only system dep. If apt is blocked by corporate firewall,
# skip APT_MIRROR and install git from the base image's default repos.
ARG APT_MIRROR_URL=""
ARG APT_MIRROR_TOKEN=""
RUN if [ -n "$APT_MIRROR_URL" ]; then \
      CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME"); \
      if [ -n "$APT_MIRROR_TOKEN" ]; then \
        REPO=$(echo "$APT_MIRROR_URL" | sed "s|://|://token:${APT_MIRROR_TOKEN}@|"); \
        SEC=$(echo "${APT_MIRROR_URL}_security" | sed "s|://|://token:${APT_MIRROR_TOKEN}@|"); \
      else \
        REPO="${APT_MIRROR_URL}"; SEC="${APT_MIRROR_URL}_security"; \
      fi; \
      rm -f /etc/apt/sources.list.d/*; \
      echo "deb [trusted=yes] ${REPO} ${CODENAME} main" > /etc/apt/sources.list; \
      echo "deb [trusted=yes] ${REPO} ${CODENAME}-updates main contrib" >> /etc/apt/sources.list; \
      echo "deb [trusted=yes] ${SEC} ${CODENAME}-security main contrib" >> /etc/apt/sources.list; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# ── Corporate PyPI mirror (optional) ─────────────────────────
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

# ── Install deps (cached layer) ─────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy app ─────────────────────────────────────────────────
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
