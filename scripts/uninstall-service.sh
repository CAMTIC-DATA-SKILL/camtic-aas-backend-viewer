#!/usr/bin/env bash
set -euo pipefail

UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VIEWER_UNIT="camtic-aas-backend-viewer.service"
PUBLISHER_UNIT="camtic-aas-publisher.service"
TARGET_UNIT="camtic-aas.target"

if [[ -f "$USER_UNIT_DIR/$VIEWER_UNIT" ]]; then
  systemctl --user disable --now "$VIEWER_UNIT" 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/$VIEWER_UNIT"
  systemctl --user daemon-reload || true
fi

# 각 서비스의 PartOf 때문에 타깃을 멈추면 화면과 발행이 같이 멈춥니다.
sudo systemctl disable --now "$TARGET_UNIT" 2>/dev/null || true
sudo systemctl disable --now "$VIEWER_UNIT" "$PUBLISHER_UNIT" 2>/dev/null || true
sudo rm -f "$UNIT_DIR/$TARGET_UNIT" "$UNIT_DIR/$VIEWER_UNIT" "$UNIT_DIR/$PUBLISHER_UNIT"
sudo systemctl daemon-reload
echo "removed $TARGET_UNIT $VIEWER_UNIT $PUBLISHER_UNIT"
