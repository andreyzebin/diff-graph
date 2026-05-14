#!/usr/bin/env bash
#
# Pull latest sources, refresh venv deps, and restart DiffGraph services.
# Run from project root as the user that owns the checkout (NOT root).
#
# Usage:
#   ./scripts/reload-services.sh                   # full: git pull + pip + restart both
#   ./scripts/reload-services.sh --no-pull         # skip git pull (e.g. only config changed)
#   ./scripts/reload-services.sh --no-pip          # skip pip install
#   ./scripts/reload-services.sh webhook           # restart only webhook
#   ./scripts/reload-services.sh qa                # restart only the QA server
#                                                  # ("trace" still accepted as an alias)
#
# After webhook.toml / config.local.yaml / benchmarks/config.local.yaml /
# .env edits, just run:
#   ./scripts/reload-services.sh --no-pull --no-pip
#
# `pip install -r requirements.txt` covers the in-repo benchmarks/
# subtree too — its deps are folded into the repo-root requirements.txt.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$INSTALL_DIR"

PULL=1
PIP=1
TARGETS=()

for arg in "$@"; do
    case "$arg" in
        --no-pull) PULL=0 ;;
        --no-pip)  PIP=0 ;;
        webhook)   TARGETS+=("diffgraph-webhook.service") ;;
        # diffgraph-trace.service was renamed to diffgraph-qa.service;
        # `trace` stays as an alias so old muscle memory / scripts work.
        qa|trace)  TARGETS+=("diffgraph-qa.service") ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
    TARGETS=("diffgraph-webhook.service" "diffgraph-qa.service")
fi

if [[ "$PULL" -eq 1 ]]; then
    echo "→ git pull"
    git pull --ff-only
fi

if [[ "$PIP" -eq 1 ]]; then
    if [[ ! -x ".venv/bin/pip" ]]; then
        echo "ERROR: .venv/bin/pip not found — create venv first" >&2
        exit 1
    fi
    echo "→ pip install -r requirements.txt"
    .venv/bin/pip install -q -r requirements.txt
fi

echo "→ sudo systemctl restart ${TARGETS[*]}"
sudo systemctl restart "${TARGETS[@]}"

echo
echo "Status:"
systemctl --no-pager --lines=0 status "${TARGETS[@]}" || true
