#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="camtic-aas-backend-viewer.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ -f "$USER_UNIT_DIR/$UNIT_NAME" ]]; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload || true
fi

sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
sudo rm -f "$UNIT_PATH"
sudo systemctl daemon-reload
echo "removed $UNIT_NAME"
