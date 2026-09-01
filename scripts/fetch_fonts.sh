#!/usr/bin/env bash
# 任意 Web フォントを取得して literaryclock/web/fonts/ に配置するスクリプト。
#
# 文学時計は OS にインストールされた日本語明朝体 (Noto Serif CJK JP など) だけでも
# 正しく動作する。このスクリプトは「Zen Old Mincho」のような、より作品の雰囲気に
# 合った書体を追加で使いたい場合のみ実行する任意ステップ。
#
# 仕組み:
#   - Google Fonts の CSS2 API から woff2 の URL を取得し、直接ダウンロードする
#   - web/js/fonts.js の FONT_SPECS に列挙されたファイル名で保存する
#   - 保存されたファイルだけが実行時に登録される (無ければ 404 を出さず OS フォントで動作)
#
# 使い方:
#   bash scripts/fetch_fonts.sh
#
# ネットワークに接続できない環境 (完全オフラインの Pi など) では、
# このスクリプトを実行せずに OS フォント (fonts-noto-cjk) だけで運用してよい。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FONT_DIR="${REPO_DIR}/literaryclock/web/fonts"
mkdir -p "${FONT_DIR}"

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

fetch_google_font () {
  local css_url="$1"
  local out_file="$2"
  echo "取得中: ${out_file}"
  local css
  css="$(curl -fsSL -A "${UA}" "${css_url}")"
  local woff2_url
  woff2_url="$(echo "${css}" | grep -o "https://[^)]*\.woff2" | tail -1)"
  if [ -z "${woff2_url}" ]; then
    echo "  警告: ${out_file} 用の woff2 URL が見つかりませんでした (スキップ)"
    return 0
  fi
  curl -fsSL "${woff2_url}" -o "${FONT_DIR}/${out_file}"
  echo "  → ${FONT_DIR}/${out_file}"
}

# Zen Old Mincho (Regular / SemiBold相当を Bold として使用)
fetch_google_font \
  "https://fonts.googleapis.com/css2?family=Zen+Old+Mincho:wght@400&text=%E6%96%87%E5%AD%97" \
  "ZenOldMincho-Regular.woff2"

fetch_google_font \
  "https://fonts.googleapis.com/css2?family=Zen+Old+Mincho:wght@600&text=%E6%96%87%E5%AD%97" \
  "ZenOldMincho-Bold.woff2"

# JetBrains Mono (HUD のデジタル時刻表示用)
fetch_google_font \
  "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400" \
  "JetBrainsMono-Regular.woff2"

echo
echo "完了。以下のファイルが存在するものだけ自動的に読み込まれます:"
ls -la "${FONT_DIR}" 2>/dev/null || true
echo
echo "注意: Zen Old Mincho は Google Fonts 側で全字種を配信していないため、"
echo "青空文庫の異体字・旧字が一部表示できない可能性があります。"
echo "その場合は OS フォント (Noto Serif CJK JP) が自動的に補完します。"
