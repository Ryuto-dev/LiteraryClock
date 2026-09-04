#!/usr/bin/env bash
# 文学時計 (LiteraryClock) — Raspberry Pi セットアップスクリプト
#
# やること:
#   1. 必要な apt パッケージを確認・インストール (chromium, フォント, unclutter,
#      表示先の指定に使う xdotool / wlr-randr / cage)
#   2. systemd サービスを配置し、自動起動させる
#      - 通常     : systemd --user (デスクトップ自動ログイン前提)
#      - --headless: システムユニット + cage (GUI 不要 / SSH 運用向け)
#
# 使い方:
#   cd LiteraryClock
#   bash scripts/install_pi.sh
#
#   # HDMI が 2 口あり、2 番目のモニタに表示したい場合:
#   bash scripts/install_pi.sh --monitor 1
#   bash scripts/install_pi.sh --monitor HDMI-2
#
#   # SSH のみで運用する / デスクトップを使わない場合 (最も確実):
#   bash scripts/install_pi.sh --headless --monitor 1
#
# 前提:
#   - 通常モード: Raspberry Pi OS で GUI autologin が有効なこと
#     (raspi-config > System Options > Boot / Auto Login > Desktop Autologin)
#   - headless モード: Console Autologin にしておくこと (GUI と競合するため)
#   - このスクリプトはリポジトリのルートから実行すること (相対パスを使うため)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="literaryclock.service"
CAGE_SERVICE_NAME="literaryclock-cage.service"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
MONITOR=""
BACKEND=""
HEADLESS=0

# --------------------------------------------------------------------------
# 0. 引数
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --monitor)
      MONITOR="${2:-}"
      shift 2
      ;;
    --monitor=*)
      MONITOR="${1#*=}"
      shift
      ;;
    --display-backend)
      BACKEND="${2:-}"
      shift 2
      ;;
    --display-backend=*)
      BACKEND="${1#*=}"
      shift
      ;;
    --headless)
      HEADLESS=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
使い方: bash scripts/install_pi.sh [--monitor SPEC] [--display-backend NAME] [--headless]

  --monitor SPEC          表示先ディスプレイを固定する。
                          番号 (0, 1) / コネクタ名 (HDMI-1, HDMI-2, HDMI-A-2) /
                          primary, left, right, top, bottom が使える。
                          省略すると OS 任せ (通常はプライマリ) になる。

  --display-backend NAME  全画面化の方法を固定する。
                          auto   環境から自動選択 (既定)
                          x11    ウィンドウを移動して WM の全画面状態にする
                          sway   sway / i3 の IPC で出力を指定する
                          wlr    labwc/wayfire で対象以外の出力を一時無効化
                          cage   GUI 不要。DRM コネクタを直接指定 (SSH 向け)
                          window 従来のブラウザ kiosk 任せ

  --headless              GUI セッションを使わず、cage による DRM 直描画で
                          自動起動させる。SSH のみで運用する場合や
                          Raspberry Pi OS Lite で使う場合はこちら。
                          (システムユニットとして /etc/systemd/system に配置)

例:
  bash scripts/install_pi.sh --monitor 1
  bash scripts/install_pi.sh --monitor HDMI-2
  bash scripts/install_pi.sh --headless --monitor 1
USAGE
      exit 0
      ;;
    *)
      echo "不明なオプション: $1  (--help で使い方を表示)" >&2
      exit 2
      ;;
  esac
done

if [ "${HEADLESS}" -eq 1 ] && [ -z "${BACKEND}" ]; then
  BACKEND="cage"
fi

echo "=== 文学時計 セットアップ ==="
echo "リポジトリ: ${REPO_DIR}"
if [ "${HEADLESS}" -eq 1 ]; then
  echo "モード    : headless (cage / DRM 直描画)"
else
  echo "モード    : デスクトップセッション (systemd --user)"
fi
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

# 表示先ディスプレイの指定に使うツール。
# SSH 経由でも狙った画面に出せるかどうかはこれらの有無で決まる。
if [ "${HEADLESS}" -eq 1 ] || [ "${BACKEND}" = "cage" ]; then
  command -v cage >/dev/null 2>&1 || NEED_PKGS+=("cage")
else
  # Wayland (Bookworm 既定) では出力の切り替えに wlr-randr が要る
  command -v wlr-randr >/dev/null 2>&1 || NEED_PKGS+=("wlr-randr")
  # X11 ではウィンドウ移動 + 全画面化に xdotool が要る
  command -v xdotool >/dev/null 2>&1 || NEED_PKGS+=("xdotool")
  # 保険として cage も入れておくと SSH 経由で確実に表示できる
  command -v cage >/dev/null 2>&1 || NEED_PKGS+=("cage")
fi

if [ "${#NEED_PKGS[@]}" -gt 0 ]; then
  echo "以下のパッケージをインストールします: ${NEED_PKGS[*]}"
  sudo apt update
  sudo apt install -y "${NEED_PKGS[@]}" || \
    echo "  警告: 一部のパッケージのインストールに失敗しました (続行します)"
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
# 3. ディスプレイの確認 (Raspberry Pi は HDMI が 2 系統)
# --------------------------------------------------------------------------
echo "--- 接続中のディスプレイ ---"
(cd "${REPO_DIR}" && python3 -m literaryclock monitors 2>/dev/null) || \
  echo "  (検出できませんでした。python3 -m literaryclock doctor で診断できます)"
echo

if [ -n "${MONITOR}" ]; then
  echo "表示先ディスプレイを '${MONITOR}' に固定します。"
else
  echo "表示先ディスプレイ: OS 任せ (通常はプライマリ)"
  echo "  別の画面に出したい場合は: bash scripts/install_pi.sh --monitor 1"
fi
if [ -n "${BACKEND}" ]; then
  echo "全画面表示バックエンド: ${BACKEND}"
fi
echo

# --------------------------------------------------------------------------
# 4. systemd サービスの配置
# --------------------------------------------------------------------------
if [ "${HEADLESS}" -eq 1 ]; then
  # --- headless: cage によるシステムユニット ---
  TARGET="/etc/systemd/system/${CAGE_SERVICE_NAME}"
  RUN_UID="$(id -u)"

  sudo sed \
    -e "s#%%USER%%#${USER}#g" \
    -e "s#%%REPO%%#${REPO_DIR}#g" \
    -e "s#^Environment=XDG_RUNTIME_DIR=.*#Environment=XDG_RUNTIME_DIR=/run/user/${RUN_UID}#" \
    -e "s#^Environment=LITCLOCK_MONITOR=.*#Environment=LITCLOCK_MONITOR=${MONITOR}#" \
    -e "s#^Environment=LITCLOCK_DISPLAY_BACKEND=.*#Environment=LITCLOCK_DISPLAY_BACKEND=${BACKEND}#" \
    -e "s#^ExecStart=/usr/bin/python3#ExecStart=$(command -v python3)#" \
    "${REPO_DIR}/systemd/${CAGE_SERVICE_NAME}" | sudo tee "${TARGET}" >/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable "${CAGE_SERVICE_NAME}"

  echo "システムユニットを配置しました: ${TARGET}"
  echo
  echo "注意: デスクトップ自動ログインが有効だと画面を取り合います。"
  echo "  sudo raspi-config → System Options → Boot / Auto Login → Console Autologin"
  echo "  に変更しておいてください。"
  echo
  echo "=== セットアップ完了 ==="
  echo "今すぐ試す:"
  echo "  sudo systemctl start ${CAGE_SERVICE_NAME}"
  echo "  journalctl -u literaryclock-cage -f   # ログを見る"
  echo
  echo "表示先の画面をあとから変える:"
  echo "  python3 -m literaryclock monitors                        # 番号を確認"
  echo "  bash scripts/install_pi.sh --headless --monitor 1        # 再実行するだけ"
  echo
  echo "再起動後から自動的に全画面で起動します (GUI ログイン不要)。"
  exit 0
fi

# --- 通常: デスクトップセッション上の systemd --user ---
mkdir -p "${USER_UNIT_DIR}"
sed \
  -e "s#%h/LiteraryClock#${REPO_DIR}#g" \
  -e "s#/usr/bin/python3 -m literaryclock#$(command -v python3) -m literaryclock#g" \
  -e "s#^Environment=LITCLOCK_MONITOR=.*#Environment=LITCLOCK_MONITOR=${MONITOR}#" \
  -e "s#^Environment=LITCLOCK_DISPLAY_BACKEND=.*#Environment=LITCLOCK_DISPLAY_BACKEND=${BACKEND}#" \
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
echo "表示先の画面をあとから変える:"
echo "  python3 -m literaryclock monitors        # 番号とコネクタ名を確認"
echo "  bash scripts/install_pi.sh --monitor 1   # 再実行するだけ"
echo
echo "意図した画面に出ないとき (SSH 経由でも診断できます):"
echo "  python3 -m literaryclock doctor --monitor 1"
echo
echo "次回 GUI ログイン時から自動的に全画面で起動します。"
