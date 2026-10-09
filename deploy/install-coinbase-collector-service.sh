#!/usr/bin/env bash

set -Eeuo pipefail

# Install the continuous public Coinbase WebSocket collector as a Raspberry Pi
# systemd service.  This service is intentionally independent of strategy and
# order-execution processes.
SERVICE_NAME="coinbase-tick-collector"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ "$(uname -s)" == "Linux" ]] \
    || fail "run this installer on the Raspberry Pi, not on macOS or Windows"
command -v systemctl >/dev/null 2>&1 || fail "systemd is not installed"
command -v sudo >/dev/null 2>&1 || fail "sudo is required to install the service"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is not installed"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
    || fail "Python 3.11 or newer is required"

# Reject characters requiring additional systemd escaping.  This ensures the
# generated unit always addresses the intended checkout and output directory.
if [[ "${PROJECT_DIR}" == *%* || "${PROJECT_DIR}" == *\$* || "${PROJECT_DIR}" == *\"* || "${PROJECT_DIR}" == *\\* || "${PROJECT_DIR}" == *$'\n'* ]]; then
    fail "the project path contains characters unsupported by this installer"
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
    printf 'Creating the virtual environment...\n'
    python3 -m venv "${PROJECT_DIR}/.venv" \
        || fail "could not create .venv; install python3-venv and retry"
fi

printf 'Installing the application into .venv...\n'
"${VENV_PYTHON}" -m pip install -e "${PROJECT_DIR}"
mkdir -p "${PROJECT_DIR}/data/raw/coinbase" "${PROJECT_DIR}/logs"

UNIT_FILE="$(mktemp)"
trap 'rm -f -- "${UNIT_FILE}"' EXIT

{
    printf '%s\n' \
        '[Unit]' \
        'Description=Coinbase public trade and Level 2 collector' \
        'Wants=network-online.target' \
        'After=network-online.target' \
        'StartLimitIntervalSec=0' \
        '' \
        '[Service]' \
        'Type=simple' \
        "User=${SERVICE_USER}" \
        "Group=${SERVICE_GROUP}" \
        "WorkingDirectory=${PROJECT_DIR}" \
        "Environment=CRYPTO_TRADER_LOG_FILE=${PROJECT_DIR}/logs/coinbase-collector.log" \
        'Environment=PYTHONUNBUFFERED=1' \
        "ExecStart=\"${VENV_PYTHON}\" -m crypto_trader.data.coinbase_ticks --products BTC-USD ETH-USD SOL-USD DOGE-USD XRP-USD --rotate-minutes 60" \
        'Restart=always' \
        'RestartSec=5' \
        'TimeoutStopSec=30' \
        'KillSignal=SIGTERM' \
        'UMask=0077' \
        'NoNewPrivileges=true' \
        'PrivateTmp=true' \
        '' \
        '[Install]' \
        'WantedBy=multi-user.target'
} >"${UNIT_FILE}"

printf 'Installing %s...\n' "${UNIT_PATH}"
sudo install -o root -g root -m 0644 "${UNIT_FILE}" "${UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

printf '\nCollector installed and enabled at boot. Current status:\n'
sudo systemctl --no-pager --full status "${SERVICE_NAME}.service"
printf '\nFollow its output with:\n  sudo journalctl -u %s -f\n' "${SERVICE_NAME}.service"

