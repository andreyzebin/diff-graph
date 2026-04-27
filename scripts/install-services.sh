#!/usr/bin/env bash
#
# Install DiffGraph systemd units (webhook + trace server) on RHEL/CentOS/Rocky/Alma.
#
# Usage:
#   sudo ./scripts/install-services.sh                # install & enable both
#   sudo INSTALL_USER=diffgraph ./scripts/install-services.sh
#   sudo ./scripts/install-services.sh --no-enable    # install only, don't start
#
# What it does:
#   1. Substitutes __INSTALL_DIR__ and __USER__ in unit templates
#   2. Writes units to /etc/systemd/system/
#   3. Runs `systemctl daemon-reload`
#   4. Enables + starts services (unless --no-enable)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_USER="${INSTALL_USER:-${SUDO_USER:-$(id -un)}}"
INSTALL_GROUP="${INSTALL_GROUP:-$(id -gn "$INSTALL_USER" 2>/dev/null || echo "$INSTALL_USER")}"
SYSTEMD_DIR="/etc/systemd/system"

ENABLE=1
for arg in "$@"; do
    case "$arg" in
        --no-enable) ENABLE=0 ;;
        -h|--help)
            sed -n '2,14p' "$0"
            exit 0
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo)" >&2
    exit 1
fi

if ! id "$INSTALL_USER" &>/dev/null; then
    echo "ERROR: user '$INSTALL_USER' does not exist" >&2
    exit 1
fi

if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
    echo "ERROR: ${INSTALL_DIR}/.venv/bin/python not found — create venv first:" >&2
    echo "  cd ${INSTALL_DIR} && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -f "${INSTALL_DIR}/webhook.toml" ]]; then
    echo "WARNING: ${INSTALL_DIR}/webhook.toml not found — webhook service will fail until you create it" >&2
    echo "         cp webhook/config.example.toml webhook.toml && edit it" >&2
fi

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    echo "WARNING: ${INSTALL_DIR}/.env not found — services will fail until you create it" >&2
    echo "         cp .env.example .env && edit it" >&2
fi

install_unit() {
    local template="$1"
    local name
    name="$(basename "$template")"
    local dest="${SYSTEMD_DIR}/${name}"
    echo "Installing ${name} → ${dest}"
    sed \
        -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
        -e "s|__USER__|${INSTALL_USER}|g" \
        -e "s|__GROUP__|${INSTALL_GROUP}|g" \
        "$template" > "$dest"
    chmod 644 "$dest"
}

echo "User=${INSTALL_USER}  Group=${INSTALL_GROUP}"

install_unit "${SCRIPT_DIR}/diffgraph-webhook.service"
install_unit "${SCRIPT_DIR}/diffgraph-trace.service"

systemctl daemon-reload

if [[ "$ENABLE" -eq 1 ]]; then
    systemctl enable --now diffgraph-webhook.service diffgraph-trace.service
    echo
    echo "Services installed and started. Status:"
    systemctl --no-pager status diffgraph-webhook.service diffgraph-trace.service || true
else
    echo
    echo "Services installed (not started). Start manually:"
    echo "  sudo systemctl enable --now diffgraph-webhook.service diffgraph-trace.service"
fi
