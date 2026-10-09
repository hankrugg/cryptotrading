#!/usr/bin/env bash

set -Eeuo pipefail

# Install a separate timer that copies finalized data to an existing rclone
# remote every fifteen minutes.  It does not delete local files by default.
SERVICE_NAME="coinbase-data-upload"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}.timer"
REMOTE="${1:-}"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ "$(uname -s)" == "Linux" ]] \
    || fail "run this installer on the Raspberry Pi, not on macOS or Windows"
[[ -n "${REMOTE}" ]] \
    || fail "usage: $0 gdrive:coinbase-data/raw"
# Restrict the value embedded in the unit to a normal rclone remote/path.
[[ "${REMOTE}" =~ ^[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+$ ]] \
    || fail "remote must look like gdrive:coinbase-data/raw without spaces"
command -v systemctl >/dev/null 2>&1 || fail "systemd is not installed"
command -v sudo >/dev/null 2>&1 || fail "sudo is required to install the timer"
command -v rclone >/dev/null 2>&1 \
    || fail "rclone is not installed; install and configure it first"
[[ -x "${VENV_PYTHON}" ]] \
    || fail "${VENV_PYTHON} is missing; install the collector service first"

REMOTE_NAME="${REMOTE%%:*}:"
sudo -H -u "${SERVICE_USER}" rclone listremotes | grep -Fxq "${REMOTE_NAME}" \
    || fail "rclone remote ${REMOTE_NAME} is not configured for ${SERVICE_USER}"

if [[ "${PROJECT_DIR}" == *%* || "${PROJECT_DIR}" == *\$* || "${PROJECT_DIR}" == *\"* || "${PROJECT_DIR}" == *\\* || "${PROJECT_DIR}" == *$'\n'* ]]; then
    fail "the project path contains characters unsupported by this installer"
fi

SERVICE_FILE="$(mktemp)"
TIMER_FILE="$(mktemp)"
trap 'rm -f -- "${SERVICE_FILE}" "${TIMER_FILE}"' EXIT

{
    printf '%s\n' \
        '[Unit]' \
        'Description=Copy finalized Coinbase data to Google Drive' \
        'Wants=network-online.target' \
        'After=network-online.target' \
        '' \
        '[Service]' \
        'Type=oneshot' \
        "User=${SERVICE_USER}" \
        "Group=${SERVICE_GROUP}" \
        "WorkingDirectory=${PROJECT_DIR}" \
        "Environment=CRYPTO_TRADER_LOG_FILE=${PROJECT_DIR}/logs/coinbase-upload.log" \
        "ExecStart=\"${VENV_PYTHON}\" -m crypto_trader.data.upload --remote ${REMOTE}" \
        'Nice=10' \
        'IOSchedulingClass=idle' \
        'UMask=0077' \
        'NoNewPrivileges=true' \
        'PrivateTmp=true'
} >"${SERVICE_FILE}"

{
    printf '%s\n' \
        '[Unit]' \
        'Description=Upload finalized Coinbase data every fifteen minutes' \
        '' \
        '[Timer]' \
        'OnBootSec=5min' \
        'OnUnitActiveSec=15min' \
        'Persistent=true' \
        'RandomizedDelaySec=60' \
        '' \
        '[Install]' \
        'WantedBy=timers.target'
} >"${TIMER_FILE}"

sudo install -o root -g root -m 0644 "${SERVICE_FILE}" "${SERVICE_PATH}"
sudo install -o root -g root -m 0644 "${TIMER_FILE}" "${TIMER_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.timer"

printf '\nUpload timer installed. Current schedule:\n'
sudo systemctl --no-pager list-timers "${SERVICE_NAME}.timer"
printf '\nRun an upload immediately with:\n  sudo systemctl start %s.service\n' "${SERVICE_NAME}"
