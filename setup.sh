#!/usr/bin/env bash
# Setup script for macOS
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_SCRIPT="$SCRIPT_DIR/main.py"
PLIST_LABEL="com.elgato-meeting-light"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if [[ "$1" == "--uninstall" ]]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Uninstalled."
    exit 0
fi

# Find Python 3
PYTHON=$(command -v python3 || command -v python)
if [[ -z "$PYTHON" ]]; then
    echo "Python 3 not found. Install it with: brew install python"
    exit 1
fi
echo "Python: $PYTHON"

# Install dependencies
echo "Installing dependencies..."
"$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

# Smoke test
echo "Running smoke test (make sure your Key Light is powered on)..."
"$PYTHON" "$MAIN_SCRIPT" --test

# Write LaunchAgent plist
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$MAIN_SCRIPT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/elgato-light.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/elgato-light.log</string>
</dict>
</plist>
EOF

# Load it now
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo "Done. LaunchAgent registered — will run at every login."
echo "Logs: $SCRIPT_DIR/elgato-light.log"
echo ""
echo "To uninstall: bash setup.sh --uninstall"
