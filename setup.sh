#!/usr/bin/env bash
# Install or reinstall the Elgato meeting light on macOS.
# Safe to run again: it replaces the LaunchAgent and syncs the light to the
# camera (the light stays off unless the camera is actually in use).
set -euo pipefail

PLIST_LABEL="com.elgato-meeting-light"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs"
APP_LOG="${LOG_DIR}/elgato-meeting-light.log"
LAUNCHD_OUT="${LOG_DIR}/elgato-meeting-light.launchd.out.log"
LAUNCHD_ERR="${LOG_DIR}/elgato-meeting-light.launchd.err.log"
REPO_URL="https://github.com/talent-collective/elgato-meeting-light.git"
DEFAULT_HOME="${ELGATO_MEETING_LIGHT_HOME:-${HOME}/.elgato-meeting-light}"

uninstall() {
    local domain="gui/$(id -u)"
    launchctl bootout "${domain}/${PLIST_LABEL}" 2>/dev/null || true
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
    rm -f "${PLIST_PATH}"
    echo "Uninstalled ${PLIST_LABEL}."
    echo "Log (left in place): ${APP_LOG}"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

# When this file is executed from a checkout it installs that checkout.
# When it is piped (curl | bash) there is no checkout beside the script, so
# clone or update ~/.elgato-meeting-light and run the setup that ships there.
resolve_source_dir() {
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        local src
        src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "${src}/main.py" ]]; then
            printf '%s\n' "${src}"
            return
        fi
    fi
    printf '\n'
}

SOURCE_DIR="$(resolve_source_dir)"
if [[ -z "${SOURCE_DIR}" ]]; then
    if [[ -d "${DEFAULT_HOME}/.git" ]]; then
        git -C "${DEFAULT_HOME}" pull --ff-only
    else
        git clone "${REPO_URL}" "${DEFAULT_HOME}"
    fi
    exec /usr/bin/env bash "${DEFAULT_HOME}/setup.sh" "$@"
fi

BOOTSTRAP="$(command -v python3 || true)"
if [[ -z "${BOOTSTRAP}" ]]; then
    echo "Python 3 not found. Install it with: brew install python" >&2
    exit 1
fi

echo "Python: ${BOOTSTRAP}"
echo "Installing into ${SOURCE_DIR}"
"${BOOTSTRAP}" -m venv "${SOURCE_DIR}/.venv"
VENV_PY="${SOURCE_DIR}/.venv/bin/python"
"${VENV_PY}" -m pip install --upgrade pip >/dev/null 2>&1 || true
"${VENV_PY}" -m pip install -r "${SOURCE_DIR}/requirements.txt"

# Stop a previous agent before syncing, so an old build cannot turn the
# light back on after we set it to the real camera state.
DOMAIN="gui/$(id -u)"
launchctl bootout "${DOMAIN}/${PLIST_LABEL}" 2>/dev/null || true
launchctl unload "${PLIST_PATH}" 2>/dev/null || true
sleep 0.3

mkdir -p "${LOG_DIR}" "${HOME}/Library/LaunchAgents"
"${VENV_PY}" - "${PLIST_PATH}" "${PLIST_LABEL}" "${VENV_PY}" "${SOURCE_DIR}/main.py" "${SOURCE_DIR}" "${LAUNCHD_OUT}" "${LAUNCHD_ERR}" <<'PY'
import plistlib
import sys

path, label, python, script, workdir, out_log, err_log = sys.argv[1:]
plist = {
    "Label": label,
    "ProgramArguments": [python, script],
    "WorkingDirectory": workdir,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "StandardOutPath": out_log,
    "StandardErrorPath": err_log,
    "EnvironmentVariables": {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONUNBUFFERED": "1",
    },
}
with open(path, "wb") as handle:
    plistlib.dump(plist, handle)
PY

echo "Syncing the light to the current camera state (stays off unless the camera is in use)..."
if ! "${VENV_PY}" "${SOURCE_DIR}/main.py" --sync; then
    echo "Sync reported an error. The background service will keep trying. See ${APP_LOG}" >&2
fi

launchctl enable "${DOMAIN}/${PLIST_LABEL}" 2>/dev/null || true
if ! launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"; then
    echo "launchctl bootstrap failed; trying legacy launchctl load" >&2
    launchctl load -w "${PLIST_PATH}"
fi

echo ""
echo "Installed."
echo "LaunchAgent: ${PLIST_LABEL}"
echo "Plist:       ${PLIST_PATH}"
echo "Log:         ${APP_LOG}"
echo "If it will not start: ${LAUNCHD_ERR}"
echo ""
echo "The light is on only while the camera is in use."
echo "Running this command again reinstalls and re-syncs."
echo ""
echo "Permissions:"
echo "  Local Network — allow it if macOS prompts, so Python can reach the Key Light."
echo "  Full Disk Access — only if the log says the system log is not permitted."
echo "    Add: ${VENV_PY}"
echo "  Camera — do not grant it. This tool never opens the camera."
echo ""
echo "Uninstall: bash \"${SOURCE_DIR}/setup.sh\" --uninstall"
