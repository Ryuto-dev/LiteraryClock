#!/usr/bin/env bash
# 文学時計 (LiteraryClock) — Raspberry Pi セットアップスクリプト
#
# やること:
#   1. 必要な apt パッケージを確認・インストール (chromium, フォント, unclutter)
#   2. systemd --user サービスを配置し、GUI ログイン時に自動起動させる
#
# 使い方:
#   cd LiteraryClock
#   bash scripts/install_pi.sh
#
# 前提:
#   - Raspberry Pi OS (Bookworm 以降推奨) で GUI autologin が有効なこと
#     (raspi-config > System Options > Boot / Auto Login > Desktop Autologin)
#   - このスクリプトはリポジトリのルートから実行すること (相対パスを使うため)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="literaryclock.service"
USER_UNIT_DIR="${HOME}/.config/systemd/user"

echo "=== 文学時計 セットアップ ==="
echo "リポジトリ: ${REPO_DIR}"
echo

# --------------------------------------------------------------------------
# 1. 必要パッケージ
# --------------------------------------------------------------------------
NEED_PKGS=()

command -v chromium-browser >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 \
  || NEED_PKGS+=("chromium-browser")

fc-list 2>/dev/null | grep -qi "Noto Serif CJK JP\|Noto Serif JP" \
  || NEED_PKGS+=("fonts-noto-cjk")

command -v unclutter >/dev/null 2>&1 || NEED_PKGS+=("unclutter")

if [ "${#NEED_PKGS[@]}" -gt 0 ]; then
  echo "以下のパッケージをインストールします: ${NEED_PKGS[*]}"
  sudo apt update
  sudo apt install -y "${NEED_PKGS[@]}"
else
  echo "必要なパッケージは既にインストールされています。"
fi
echo

# --------------------------------------------------------------------------
# 2. データセットの確認
# --------------------------------------------------------------------------
DATASET="${REPO_DIR}/data/literary_clock.json"
if [ -f "${DATASET}" ]; then
  ENTRIES=$(python3 -c "import json;print(len(json.load(open('${DATASET}',encoding='utf-8'))))" 2>/dev/null || echo "?")
  echo "データセットを検出: ${DATASET} (${ENTRIES} 件)"
else
  echo "警告: ${DATASET} が見つかりません。"
  echo "  data/literary_clock.json を配置してから起動してください。"
fi
echo

# --------------------------------------------------------------------------
# 3. systemd --user サービスの配置
# --------------------------------------------------------------------------
mkdir -p "${USER_UNIT_DIR}"
sed \
  -e "s#%h/LiteraryClock#${REPO_DIR}#g" \
  -e "s#/usr/bin/python3 -m literaryclock#$(command -v python3) -m literaryclock#g" \
  "${REPO_DIR}/systemd/${SERVICE_NAME}" > "${USER_UNIT_DIR}/${SERVICE_NAME}"

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"

echo "systemd --user サービスを配置しました: ${USER_UNIT_DIR}/${SERVICE_NAME}"
echo

# 再起動後も自動起動させるには linger が必要
if command -v loginctl >/dev/null 2>&1; then
  if ! loginctl show-user "${USER}" 2>/dev/null | grep -q "Linger=yes"; then
    echo "再起動後も自動起動させるため linger を有効化します (sudo が必要です):"
    sudo loginctl enable-linger "${USER}" || \
      echo "  → 失敗しました。手動で: sudo loginctl enable-linger ${USER}"
  fi
fi
echo

echo "=== セットアップ完了 ==="
echo "今すぐ試す:"
echo "  systemctl --user start ${SERVICE_NAME}"
echo "  journalctl --user -u literaryclock -f   # ログを見る"
echo
echo "次回 GUI ログイン時から自動的に全画面で起動します。"
