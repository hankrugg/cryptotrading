#!/usr/bin/env bash

set -Eeuo pipefail

# The installer creates only the hourly signal service. The candle collector
# and risk monitor are separate commands and need their own units if you want
# them to run continuously on the Pi.
SERVICE_NAME="crypto-trader"
# Resolve paths from this script's location so it works from any shell folder.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
ENV_FILE="${PROJECT_DIR}/.env"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

fail() {
    # Every validation failure exits with a short actionable message.
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

# systemd and this unit file are intended to be installed on the Pi, not on a
# development laptop running macOS or Windows.
if [[ "$(uname -s)" != "Linux" ]]; then
    fail "run this installer on the Raspberry Pi, not on macOS or Windows"
fi

# Check the tools before creating a virtual environment or asking for sudo.
command -v systemctl >/dev/null 2>&1 || fail "systemd is not installed"
command -v sudo >/dev/null 2>&1 || fail "sudo is required to install the service"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is not installed"

python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
    || fail "Python 3.11 or newer is required"

# These characters need additional escaping in systemd unit values. Rejecting
# them keeps the generated unit compatible with Raspberry Pi OS systemd.
# systemd interprets several characters specially in unit values. Rejecting
# them is safer than generating a unit that points at the wrong directory.
if [[ "${PROJECT_DIR}" == *%* || "${PROJECT_DIR}" == *\$* || "${PROJECT_DIR}" == *\"* || "${PROJECT_DIR}" == *\\* || "${PROJECT_DIR}" == *$'\n'* ]]; then
    fail "the project path cannot contain percent signs, dollar signs, quotes, backslashes, or newlines: ${PROJECT_DIR}"
fi

# Create the project's isolated Python environment only if it is missing.
if [[ ! -x "${VENV_PYTHON}" ]]; then
    printf 'Creating the virtual environment...\n'
    python3 -m venv "${PROJECT_DIR}/.venv" \
        || fail "could not create .venv; install the python3-venv OS package and retry"
fi

"${VENV_PYTHON}" -c 'import sys; print(sys.version)' >/dev/null 2>&1 \
    || fail "the existing .venv does not work on this Pi; move it aside and rerun this installer"

# Editable installation makes the service import the checked-out source tree;
# after a code update, reinstalling refreshes package metadata/entry points.
printf 'Installing the application into .venv...\n'
"${VENV_PYTHON}" -m pip install -e "${PROJECT_DIR}"

# The service needs the Gmail app password. Do not copy the secret into the
# generated unit file; systemd reads it through the process environment only
# after main.py loads .env.
[[ -f "${ENV_FILE}" ]] \
    || fail "${ENV_FILE} is missing; create it with GMAIL_APP_PASSWORD before retrying"
grep -Eq '^[[:space:]]*GMAIL_APP_PASSWORD[[:space:]]*=' "${ENV_FILE}" \
    || fail "GMAIL_APP_PASSWORD is missing from ${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# Write the unit to a temporary file first, then install it atomically as root.
UNIT_FILE="$(mktemp)"
trap 'rm -f -- "${UNIT_FILE}"' EXIT

{
    # [Unit] controls boot ordering. Waiting for network-online avoids an
    # immediate Yahoo/email failure during startup. The comments must stay
    # outside the continued printf argument list below.
    printf '%s\n' \
        '[Unit]' \
        'Description=Crypto Trader hourly signal runner' \
        'Wants=network-online.target' \
        'After=network-online.target' \
        '' \
        '[Service]' \
        'Type=simple' \
        "User=${SERVICE_USER}" \
        "Group=${SERVICE_GROUP}" \
        "WorkingDirectory=${PROJECT_DIR}" \
        "ExecStart=\"${VENV_PYTHON}\" -m crypto_trader" \
        'Environment=PYTHONUNBUFFERED=1' \
        'Restart=on-failure' \
        'RestartSec=30' \
        'NoNewPrivileges=true' \
        'PrivateTmp=true' \
        '' \
        '[Install]' \
        'WantedBy=multi-user.target'
} >"${UNIT_FILE}"

# Install, reload systemd's unit cache, enable boot startup, and restart now.
printf 'Installing %s...\n' "${UNIT_PATH}"
sudo install -o root -g root -m 0644 "${UNIT_FILE}" "${UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

printf '\nThe service is installed and enabled at boot. Current status:\n'
sudo systemctl --no-pager --full status "${SERVICE_NAME}.service"
printf '\nFollow its live output with:\n  sudo journalctl -u %s -f\n' "${SERVICE_NAME}.service"
