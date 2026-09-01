# 文学時計 (LiteraryClock)

青空文庫の全文コーパスから抽出した「その時刻を表す一節」を、10 分ごとに全画面で表示する時計です。
Raspberry Pi での kiosk 運用を第一の目標としていますが、Python 標準ライブラリ + ブラウザだけで
動くため、Linux 全般 (将来的には Windows / macOS) でも動作します。

| ink (既定) | washi | vertical (縦書き) |
|---|---|---|
| ![ink](docs/screenshot-ink.png) | ![washi](docs/screenshot-washi.png) | ![vertical](docs/screenshot-vertical.png) |

## 特徴

- **依存パッケージなし** — サーバは Python 標準ライブラリ (`http.server`) のみで動作。
  フロントエンドはビルド不要の素の HTML/CSS/JS (ES Modules)。
- **5 種類の配色テーマ** — `ink` (墨) / `washi` (和紙) / `night` (夜間モード) /
  `sepia` (古書) / `mono` (無彩色)。
- **縦書き対応** — 横書き / 縦書き / 画面比率に応じた自動切替。縦中横・約物処理済み。
- **5 種類の切替アニメーション** — fade / typewriter (一文字ずつ) / blur / slide / none。
- **文字数に応じた自動フォントサイズ調整** — 短い引用も長い引用も画面に収まる。
- **欠損データへの耐性** — 該当時刻のデータが無い場合、直近の過去スロットで代替表示。
- **kiosk 運用を想定した堅牢性** — 通信断からの自動復旧、スリープ復帰時刻の再同期、
  累積誤差の無いスロット切替タイマー。
- **キーボードショートカット** — テーマ・組み方向・アニメーション・文字サイズなどを
  その場で調整可能 (`H` キーでヘルプ表示)。設定は端末に保存される。

## 動作環境

- Python 3.9 以上 (Raspberry Pi OS Bookworm は 3.11 を標準搭載)
- Chromium ベースのブラウザ推奨 (Raspberry Pi OS 標準の `chromium-browser`)。
  Firefox でも kiosk 起動に対応。
- 追加の pip パッケージは **不要** (標準ライブラリのみ)。

## クイックスタート (開発機での確認)

```bash
git clone https://github.com/Ryuto-dev/LiteraryClock.git
cd LiteraryClock

# ブラウザを自動起動せず、サーバだけ立てて手元のブラウザで確認する場合
python3 -m literaryclock --no-kiosk --host 0.0.0.0 --port 8730
# → http://localhost:8730/ をブラウザで開く

# データセットの健全性を確認する
python3 -m literaryclock validate data/literary_clock.json

# 現在時刻 (または指定時刻) の引用を端末で確認する
python3 -m literaryclock preview 16:40
```

## Raspberry Pi へのセットアップ

1. Raspberry Pi OS (Bookworm 以降推奨) で **デスクトップ自動ログイン** を有効化する。
   ```
   sudo raspi-config
   → System Options → Boot / Auto Login → Desktop Autologin
   ```
2. リポジトリを配置し、セットアップスクリプトを実行する。
   ```bash
   git clone https://github.com/Ryuto-dev/LiteraryClock.git ~/LiteraryClock
   cd ~/LiteraryClock
   bash scripts/install_pi.sh
   ```
   スクリプトが以下を行います。
   - `chromium-browser` / `fonts-noto-cjk` (日本語明朝) / `unclutter` の apt インストール
   - `systemd --user` サービスの配置・有効化 (次回 GUI ログインから自動起動)
   - 再起動後も自動起動させるための `loginctl enable-linger`
3. 今すぐ試したい場合:
   ```bash
   systemctl --user start literaryclock.service
   journalctl --user -u literaryclock -f   # ログを見る
   ```

手動で systemd を設定したい場合や公式 7 インチタッチパネル以外のディスプレイを使う場合は
`systemd/literaryclock.service` のコメントを参照してください。

### アンインストール

```bash
systemctl --user disable --now literaryclock.service
rm ~/.config/systemd/user/literaryclock.service
sudo loginctl disable-linger "$USER"   # 他のユーザーサービスが無ければ
```

## コマンドラインオプション

```
literary-clock                    全画面で時計を起動 (既定)
literary-clock --no-kiosk         ブラウザを起動せずサーバのみ動かす
literary-clock validate <file>    データセットを検証する
literary-clock preview 16:40      指定時刻の引用を端末に表示する
```

主なオプション (詳細は `literary-clock --help`):

| オプション | 説明 | 既定値 |
|---|---|---|
| `--dataset PATH` | `literary_clock.json` のパス | `data/literary_clock.json` |
| `--theme {ink,washi,night,sepia,mono}` | 配色テーマ | `ink` |
| `--writing-mode {horizontal,vertical,auto}` | 組み方向 | `horizontal` |
| `--transition {fade,typewriter,blur,slide,none}` | 切替アニメーション | `fade` |
| `--font-scale FLOAT` | 文字サイズ倍率 (0.4〜3.0) | `1.0` |
| `--no-credit` | 作者・作品名を非表示 | 表示する |
| `--no-digital-clock` | デジタル時刻 (HH:MM:SS) を非表示 | 表示する |
| `--fake-time HH:MM` | 時刻を固定 (デバッグ・展示用) | なし |
| `--time-speed FLOAT` | 時間の進みを N 倍速に (デモ用) | `1.0` |
| `--host / --port` | 待受アドレス | `127.0.0.1:8730` |
| `--config PATH` | 設定ファイル (JSON) | 自動探索 |

設定の優先順位: **デフォルト → 設定ファイル → 環境変数 (`LITCLOCK_*`) → コマンドライン引数**

## 画面内キーボードショートカット

kiosk 動作中でもキーボードが繋がっていれば、その場で見た目を調整できます (`H` で一覧表示)。

| キー | 動作 |
|---|---|
| `T` | テーマを切り替える |
| `W` | 縦書き / 横書き / 自動 を切り替える |
| `A` | 切替アニメーションを変更する |
| `+` / `-` | 文字サイズを調整する |
| `C` | 作者・作品名の表示切替 |
| `D` | デジタル時刻の表示切替 |
| `N` | 次の候補 / 次の時刻を表示 (デバッグ用プレビュー) |
| `R` | 再読み込み |
| `F` | ブラウザの全画面表示切替 |
| `H` / `Esc` | ヘルプの表示/非表示 |

調整した設定は `localStorage` に保存され、次回起動時も維持されます。

## データセット (`data/literary_clock.json`)

青空文庫全文コーパス (17,436 作品・1,010 作家) から、00:00〜23:50 の 10 分刻み・
全 144 スロットについて、その時刻を表す一節を抽出したデータセットです。

```json
{
  "time": "16:40",
  "excerpt": "午後四時四十分",
  "before": "で、ちょっと行き渋ったが、職業柄理由なく断わるのもよくないと思い、",
  "after": "発の急行で、東京駅を立ったのだった。",
  "quote": "（before + excerpt + after を連結したもの）",
  "author": "大倉燁子",
  "title": "深夜の客",
  "source": {
    "collection": "aozorabunko_text",
    "author_id": "001669",
    "file": "001669/files/54478_ruby_49065/54478_ruby_49065.txt"
  }
}
```

| フィールド | 内容 |
|---|---|
| `time` | `HH:MM` 形式の対象時刻 (10 分刻み、24 時間表記) |
| `excerpt` | 原文中でその時刻を表している箇所そのもの |
| `before` | `excerpt` 直前の文脈 (句読点・文頭などの区切りまで) |
| `after` | `excerpt` 直後の文脈 |
| `quote` | `before + excerpt + after` を単純結合した完全な引用文 |
| `author` | 作家名 (翻訳作品は訳者名の場合あり) |
| `title` | 作品名 |
| `source.author_id` | 青空文庫の作家フォルダ ID |
| `source.file` | 元データ内でのファイルパス |

`data/literary_clock.json` に本物のデータセットを配置してください。手元に無い場合、
`scripts/make_sample_dataset.py` で動作確認用のプレースホルダ (144 件、著者名は
「（サンプル）」) を生成できます。

```bash
python3 scripts/make_sample_dataset.py -o data/literary_clock.sample.json
literary-clock --dataset data/literary_clock.sample.json
```

**データが欠損しているスロットがあっても動作します。** 直近の過去スロットの引用で
自動的に代替表示されます (日付を跨いで遡ります)。`literary-clock validate` で
欠損状況を確認できます。

## Web フォント (任意)

既定では OS にインストールされた日本語明朝体 (`Noto Serif CJK JP` など) で表示されます。
より作品の雰囲気に合った書体 (Zen Old Mincho) を使いたい場合:

```bash
bash scripts/fetch_fonts.sh
```

`literaryclock/web/fonts/` に該当ファイルが実際に存在する場合のみ、実行時に登録されます
(存在しない場合に 404 を出さないよう `/api/fonts` で存在確認してから読み込みます)。

## アーキテクチャ

```
literaryclock/
  cli.py         コマンドラインインタフェース (run / validate / preview)
  config.py      設定の読込・マージ (デフォルト → ファイル → 環境変数 → CLI)
  dataset.py     literary_clock.json の読込・検証・時刻引き当て
  server.py      静的配信 + JSON API (http.server ベース、依存なし)
  display.py     kiosk ブラウザ起動・画面消灯無効化 (X11/Wayland 両対応)
  web/
    index.html   画面構造
    css/         テーマ・レイアウト・アニメーション (CSS 変数でテーマ切替)
    js/
      app.js     状態管理・タイマー・キーボード操作
      render.js  引用の描画・文字サイズ自動調整・typewriter アニメーション
      api.js     サーバとの通信 (fetch ラッパ)
      fonts.js   任意 Web フォントの実行時登録
```

サーバは `/api/bootstrap` `/api/now` `/api/at` `/api/health` `/api/fonts` の
JSON API を提供し、フロントエンドはこれをポーリングして 10 分スロットの境界で
引用を差し替えます (`setInterval` の累積誤差を避け、毎回「次の境界までの残り時間」を
再計算する方式)。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/` には設定・データセット・サーバ (実際に HTTP サーバを起動して検証) の
122 件のテストがあります。`scripts/shots.py` は Playwright で各テーマ・組み方向の
スクリーンショットを撮る開発用ツールです。

## トラブルシューティング

- **ブラウザが起動しない** — `chromium-browser` (または `chromium`) がインストール
  されているか確認してください。`--no-kiosk` でサーバのみ起動し、表示された URL を
  手動でブラウザで開くことでも動作確認できます。
- **文字が豆腐 (□) になる** — `sudo apt install fonts-noto-cjk` を実行してください。
- **Wayland (labwc) で画面がちらつく / 起動しない** — Raspberry Pi OS Bookworm は
  既定で Wayland です。本ソフトは `WAYLAND_DISPLAY` を検出して Chromium に
  `--ozone-platform=wayland` を付与しますが、うまくいかない場合は
  `raspi-config` で X11 に切り替えることも可能です。
- **ポートが使用中というエラー** — `--port` で別の待受ポートを指定してください。

## ライセンス

MIT License. 詳細は [LICENSE](LICENSE) を参照してください。
同梱データセットの引用文自体の著作権については、青空文庫の各作品の「図書カード」の
記載に従ってください。
