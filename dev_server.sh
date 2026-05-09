#!/usr/bin/env bash
# Dev runner: starts the trace/quality server with auto-reload, kills any
# existing instance on the same port, writes a pidfile, and tails the log.
#
# Usage:
#   ./dev_server.sh           # start in foreground (Ctrl-C stops)
#   ./dev_server.sh start     # start in background (logs/server.log, .server.pid)
#   ./dev_server.sh stop      # stop background instance
#   ./dev_server.sh restart   # stop + start
#   ./dev_server.sh status    # show pid + recent log
#   ./dev_server.sh logs      # tail log

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
HOST="${HOST:-0.0.0.0}"
APP="tracing.server.app:app"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/server.log"
PID_FILE=".server.pid"

mkdir -p "$LOG_DIR"

kill_port() {
  # Best-effort kill: PID file → port lookup → done.
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # Anything still bound to the port (e.g. a previous foreground run)
  local on_port
  on_port=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "$on_port" ]]; then
    kill $on_port 2>/dev/null || true
    sleep 0.3
    kill -9 $on_port 2>/dev/null || true
  fi
}

start_fg() {
  kill_port
  exec .venv/bin/python -m uvicorn "$APP" \
    --host "$HOST" --port "$PORT" \
    --reload \
    --reload-dir tracing \
    --reload-dir quality_api \
    --reload-include '*.py' --reload-include '*.html' \
    --reload-include '*.js'  --reload-include '*.css'
}

start_bg() {
  kill_port
  nohup .venv/bin/python -m uvicorn "$APP" \
    --host "$HOST" --port "$PORT" \
    --reload \
    --reload-dir tracing \
    --reload-dir quality_api \
    --reload-include '*.py' --reload-include '*.html' \
    --reload-include '*.js'  --reload-include '*.css' \
    >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 0.4
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "started: pid=$(cat "$PID_FILE")  port=$PORT  log=$LOG_FILE"
  else
    echo "failed to start — see $LOG_FILE"
    tail -20 "$LOG_FILE" || true
    exit 1
  fi
}

stop_bg() {
  kill_port
  echo "stopped"
}

status() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "running: pid=$(cat "$PID_FILE")  port=$PORT"
  else
    echo "not running"
  fi
  if [[ -f "$LOG_FILE" ]]; then
    echo "--- last 10 log lines ---"
    tail -10 "$LOG_FILE"
  fi
}

case "${1:-fg}" in
  fg|foreground|"")  start_fg ;;
  start)             start_bg ;;
  stop)              stop_bg ;;
  restart)           stop_bg; start_bg ;;
  status)            status ;;
  logs)              tail -f "$LOG_FILE" ;;
  *)
    echo "usage: $0 [fg|start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
