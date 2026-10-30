#!/usr/bin/env bash
# Install couch-controller for the current user. No sudo required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/couch-controller"
UNIT_DIR="${HOME}/.config/systemd/user"

NO_SERVICE=0
WITH_IDLE=0

usage() {
    cat <<EOF
Usage: $0 [--no-service] [--with-idle]

  --no-service   copy files but do not enable the systemd --user service
  --with-idle    also set GNOME to blank+lock after 10 minutes and never
                 suspend on AC power
  -h, --help     show this help
EOF
}

for arg in "$@"; do
    case "$arg" in
        --no-service) NO_SERVICE=1 ;;
        --with-idle) WITH_IDLE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

install -d -m 755 "$BIN_DIR"
install -d -m 755 "$UNIT_DIR"
install -d -m 700 "$CONFIG_DIR"

install -m 755 "$ROOT/couch-controller.py" "$BIN_DIR/couch-controller.py"
install -m 644 "$ROOT/couch-controller.service" "$UNIT_DIR/couch-controller.service"

if [ -e "$CONFIG_DIR/config.json" ]; then
    echo "kept existing $CONFIG_DIR/config.json"
else
    install -m 600 "$ROOT/config.example.json" "$CONFIG_DIR/config.json"
    echo "wrote $CONFIG_DIR/config.json (example combo: UP DOWN LEFT RIGHT)"
fi

if [ "$WITH_IDLE" -eq 1 ]; then
    gsettings set org.gnome.desktop.session idle-delay 600
    gsettings set org.gnome.desktop.screensaver lock-enabled true
    gsettings set org.gnome.desktop.screensaver lock-delay 0
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
    echo "applied GNOME idle/lock settings"
fi

if [ "$NO_SERVICE" -eq 0 ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now couch-controller.service
    echo "enabled couch-controller.service"
fi

echo
"$BIN_DIR/couch-controller.py" --doctor || true
echo
echo "Combo: see $CONFIG_DIR/config.json"
echo "Logs:  journalctl --user -u couch-controller.service -f"
