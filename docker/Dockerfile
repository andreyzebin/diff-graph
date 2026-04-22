ARG DOCKER_REGISTRY=""
FROM ${DOCKER_REGISTRY}python:3.13-slim

# ── Corporate CA bundle (optional, for pip SSL during build) ─
COPY docker/certs/ /usr/local/share/ca-certificates/corp/
RUN echo "=== docker/certs/ contents ===" && \
    ls -la /usr/local/share/ca-certificates/corp/ && \
    echo "=== cert fingerprints ===" && \
    for f in /usr/local/share/ca-certificates/corp/*.crt /usr/local/share/ca-certificates/corp/*.pem; do \
      [ -f "$f" ] && echo "-- $f --" && \
      openssl x509 -in "$f" -noout -subject -issuer 2>/dev/null || \
      (echo "(bundle, scanning...)" && awk '/BEGIN CERT/,/END CERT/' "$f" | openssl x509 -noout -subject -issuer 2>/dev/null | head -20); \
    done || true
RUN chmod -R 644 /usr/local/share/ca-certificates/corp/* 2>/dev/null || true
RUN echo "=== /etc/ssl/certs/ca-certificates.crt BEFORE update: $(wc -c < /etc/ssl/certs/ca-certificates.crt) bytes ===" && \
    echo "=== /usr/lib/ssl/cert.pem BEFORE: $(wc -c < /usr/lib/ssl/cert.pem 2>/dev/null || echo missing) bytes ===" && \
    update-ca-certificates && \
    echo "=== /etc/ssl/certs/ca-certificates.crt AFTER update: $(wc -c < /etc/ssl/certs/ca-certificates.crt) bytes ===" && \
    echo "=== /usr/lib/ssl/cert.pem AFTER: $(wc -c < /usr/lib/ssl/cert.pem 2>/dev/null || echo missing) bytes ==="
# Verify: /usr/lib/ssl/cert.pem is a symlink to /etc/ssl/certs/ca-certificates.crt
# on Debian, so update-ca-certificates updates both. Print proof.
RUN echo "=== /usr/lib/ssl/cert.pem:" && ls -la /usr/lib/ssl/cert.pem && \
    echo "=== /etc/ssl/certs/ca-certificates.crt:" && ls -la /etc/ssl/certs/ca-certificates.crt && \
    echo "=== corp CA subjects present in final bundle ===" && \
    awk '/BEGIN CERT/,/END CERT/' /usr/lib/ssl/cert.pem | \
      openssl crl2pkcs7 -nocrl -certfile /dev/stdin 2>/dev/null | \
      openssl pkcs7 -print_certs -noout 2>/dev/null | \
      grep -E "^(subject|issuer)" | grep -i "cplsb\|corp\|sber" || \
    echo "  (no corp CA subjects found — bundle may be incomplete)"

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
