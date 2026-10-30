#!/usr/bin/env bash
# Remove couch-controller. The config is kept unless --purge is given.
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/couch-controller"
UNIT_DIR="${HOME}/.config/systemd/user"

systemctl --user disable --now couch-controller.service 2>/dev/null || true
rm -f "$UNIT_DIR/couch-controller.service"
rm -f "$BIN_DIR/couch-controller.py"
rm -f "$BIN_DIR/couch-focus-bp"   # helper used by older versions
systemctl --user daemon-reload 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CONFIG_DIR"
    echo "removed service, script and config"
else
    echo "removed service and script"
    echo "kept $CONFIG_DIR/config.json (use --purge to delete it)"
fi

echo "GNOME idle/lock settings were left unchanged."
