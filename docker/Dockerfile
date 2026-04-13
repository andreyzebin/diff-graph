FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git grep && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

RUN mkdir -p /app/certs /data

ENV PYTHONUNBUFFERED=1
ENV WEBHOOK_CONFIG=/app/webhook.toml
ENV WEBHOOK_PORT=8000
ENV TRACE_PORT=8080

EXPOSE 8000 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
