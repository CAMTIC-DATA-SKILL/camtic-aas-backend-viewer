#!/usr/bin/env bash
# 시스템 systemd 서비스로 등록하고, 재부팅 후에도 자동 기동되게 합니다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_NAME="camtic-aas-backend-viewer.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
TEMPLATE="$ROOT/deploy/camtic-aas-backend-viewer.service.in"
PYTHON="$ROOT/.venv/bin/python"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"

if [[ ! -x "$PYTHON" ]]; then
  echo "가상환경이 없습니다: $PYTHON" >&2
  echo "먼저 python -m venv .venv && .venv/bin/pip install -r requirements.txt 를 실행하세요." >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo ".env 파일이 없습니다: $ROOT/.env" >&2
  echo ".env.example 을 복사해 설정을 만든 뒤 다시 실행하세요." >&2
  exit 1
fi

# 예전 사용자 유닛이 있으면 제거해서 systemctl --user 와 중복 기동되지 않게 합니다.
if [[ -f "$USER_UNIT_DIR/$UNIT_NAME" ]]; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload || true
fi

tmp="$(mktemp)"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$RUN_USER|g" -e "s|@GROUP@|$RUN_GROUP|g" "$TEMPLATE" > "$tmp"
sudo install -m 644 "$tmp" "$UNIT_PATH"
rm -f "$tmp"

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT_NAME"
sudo systemctl --no-pager --full status "$UNIT_NAME"
