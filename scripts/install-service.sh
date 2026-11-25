#!/usr/bin/env bash
# 화면과 발행을 각각 systemd 서비스로 등록하고, camtic-aas.target 으로 같이 기동합니다.
# 서비스 수명은 분리됩니다. 타깃을 멈추거나 재시작하면 둘 다 따라가고,
# 서비스 하나만 재시작하면 다른 쪽 OPC-UA 구독이나 화면은 유지됩니다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"
VIEWER_UNIT="camtic-aas-backend-viewer.service"
PUBLISHER_UNIT="camtic-aas-publisher.service"
TARGET_UNIT="camtic-aas.target"

if [[ ! -x "$PYTHON" ]]; then
  echo "가상환경이 없습니다: $PYTHON" >&2
  echo "먼저 python -m venv .venv && .venv/bin/pip install -r requirements.txt 를 실행하세요." >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo ".env 파일이 없습니다: $ROOT/.env" >&2
  echo "프로젝트 루트에 .env 를 만들어 설정(DATA_ROOT 등)한 뒤 다시 실행하세요." >&2
  exit 1
fi

# 예전 사용자 유닛이 있으면 제거해서 systemctl --user 와 중복 기동되지 않게 합니다.
if [[ -f "$USER_UNIT_DIR/$VIEWER_UNIT" ]]; then
  systemctl --user disable --now "$VIEWER_UNIT" 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/$VIEWER_UNIT"
  systemctl --user daemon-reload || true
fi

install_unit() {
  local template="$1"
  local name="$2"
  local tmp
  tmp="$(mktemp)"
  sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$RUN_USER|g" -e "s|@GROUP@|$RUN_GROUP|g" "$template" > "$tmp"
  sudo install -m 644 "$tmp" "$UNIT_DIR/$name"
  rm -f "$tmp"
}

install_unit "$ROOT/deploy/camtic-aas-backend-viewer.service.in" "$VIEWER_UNIT"
install_unit "$ROOT/deploy/camtic-aas-publisher.service.in" "$PUBLISHER_UNIT"
install_unit "$ROOT/deploy/camtic-aas.target.in" "$TARGET_UNIT"

sudo systemctl daemon-reload
# WantedBy 가 multi-user 에서 타깃으로 바뀌어도 예전 enable 링크가 남지 않게 다시 겁니다.
sudo systemctl reenable "$VIEWER_UNIT"
sudo systemctl reenable "$PUBLISHER_UNIT"
sudo systemctl reenable "$TARGET_UNIT"
sudo systemctl start "$TARGET_UNIT"
sudo systemctl --no-pager --full status "$TARGET_UNIT" "$VIEWER_UNIT" "$PUBLISHER_UNIT"
