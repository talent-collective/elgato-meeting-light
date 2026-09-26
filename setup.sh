#!/usr/bin/env bash
# Install or reinstall the Elgato meeting light on macOS.
# Dependencies are installed before the existing agent is stopped, so a
# failed pip leaves the previous agent running.
set -euo pipefail

PLIST_LABEL="com.elgato-meeting-light"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
REPO_URL="https://github.com/talent-collective/elgato-meeting-light.git"
DEFAULT_HOME="${ELGATO_MEETING_LIGHT_HOME:-${HOME}/.elgato-meeting-light}"

stop_agent() {
    launchctl bootout "${DOMAIN}/${PLIST_LABEL}" 2>/dev/null || true
    launchctl unload "${PLIST_PATH}" 2>/dev/null || true
}

uninstall() {
    DOMAIN="gui/$(id -u)"
    stop_agent
    rm -f "${PLIST_PATH}"
    echo "Uninstalled ${PLIST_LABEL}."
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

# Application log is elgato-light.log (FileHandler only).
# launchd stdout/stderr are different files so those streams are not copied
# into the application log a second time.
APP_LOG="${SOURCE_DIR}/elgato-light.log"
LAUNCHD_OUT="${SOURCE_DIR}/elgato-light.launchd.out.log"
LAUNCHD_ERR="${SOURCE_DIR}/elgato-light.launchd.err.log"

BOOTSTRAP="$(command -v python3 || true)"
if [[ -z "${BOOTSTRAP}" ]]; then
    echo "Python 3 not found. Install it with: brew install python" >&2
    exit 1
fi

echo "Python: ${BOOTSTRAP}"
echo "Installing into ${SOURCE_DIR}"

# Until the agent is stopped, a failure here leaves the previous install running.
trap 'echo "Not installed; your previous install is unchanged. Rerun setup.sh." >&2' ERR

"${BOOTSTRAP}" -m venv "${SOURCE_DIR}/.venv"
VENV_PY="${SOURCE_DIR}/.venv/bin/python"
if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
    "${VENV_PY}" -m ensurepip --upgrade
fi
"${VENV_PY}" -m pip install --upgrade pip >/dev/null 2>&1 || true
"${VENV_PY}" -m pip install -r "${SOURCE_DIR}/requirements.txt"

trap - ERR

# Stop the previous agent only after install succeeded, and before --test,
# so the connectivity check is the only process talking to the light.
# Installs from before the CoreMediaIO camera check treated a running
# VDCAssistant as "camera on" and could turn the light back on after that
# check. Current builds do not; the agent is still stopped first.
DOMAIN="gui/$(id -u)"
echo "Stopping any existing ${PLIST_LABEL} agent..."
stop_agent
sleep 0.3

mkdir -p "${HOME}/Library/LaunchAgents"
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

# Blink on then off to prove the light is reachable, then leave it matched
# to the camera. A camera that is off is not left with the light on.
echo "Checking the light (on, then off), then matching it to the camera..."
if ! "${VENV_PY}" "${SOURCE_DIR}/main.py" --test; then
    echo "Light check reported an error. The background service will keep trying. See ${APP_LOG}" >&2
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
echo "The light is on only while CoreMediaIO reports a camera running."
echo "Live check: \"${VENV_PY}\" \"${SOURCE_DIR}/main.py\" --probe"
echo "Running this script again reinstalls."
echo ""
echo "If macOS asks, allow Local Network access so Python can reach the Key Light."
echo "Camera access is not required."
echo ""
echo "Uninstall: bash \"${SOURCE_DIR}/setup.sh\" --uninstall"
