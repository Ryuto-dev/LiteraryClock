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
- **マルチモニタ対応** — Raspberry Pi の HDMI 2 口にモニタを 2 台繋いだ状態でも、
  `--monitor` で表示先を番号・コネクタ名・位置から簡単に指定できる。
- **SSH 経由でも狙った画面に出せる** — デスクトップ側の GUI セッションを自動で
  探して環境変数を引き継ぐほか、GUI が無くても `cage` で DRM に直接描画できる。
  全画面化はブラウザ任せではなく WM / コンポジタ側で行うため表示先が確実。
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

   # モニタを 2 台繋いでいて、 2 番目の HDMI に出したい場合
   bash scripts/install_pi.sh --monitor 1

   # SSH のみで運用する / デスクトップを入れていない場合 (cage で DRM 直描画)
   bash scripts/install_pi.sh --headless --monitor 1
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
literary-clock --monitor 1        2 番目の画面 (HDMI) に全画面表示する
literary-clock monitors           接続中のディスプレイを一覧表示する
literary-clock doctor             表示環境を診断する (SSH で映らない時はこれ)
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
| `--monitor SPEC` | 表示先ディスプレイ | OS 任せ |
| `--list-monitors` | 接続中の画面を一覧表示して終了 | — |
| `--strict-monitor` | `--monitor` が見つからない時にエラー終了 | プライマリへ退避 |
| `--window-position X,Y` | ウィンドウ位置を直接指定 | なし |
| `--window-size W,H` | ウィンドウサイズを直接指定 | なし |
| `--display-backend {auto,x11,sway,wlr,cage,window}` | 全画面化の方法 | `auto` |
| `--session SPEC` | 引き継ぐ GUI セッション (`auto`/`none`/`wayland-0`/`:0`) | `auto` |
| `--no-adopt-session` | GUI セッションの自動引き継ぎを無効化 | 引き継ぐ |
| `--no-exclusive-output` | `wlr` で他の出力を無効化しない | 無効化する |

設定の優先順位: **デフォルト → 設定ファイル → 環境変数 (`LITCLOCK_*`) → コマンドライン引数**

## モニタの指定 (HDMI 2 口を使う場合)

Raspberry Pi 4 / 5 には HDMI 出力が 2 系統あります。モニタを 2 台繋いだ状態でも、
どちらに時計を出すかを `--monitor` で指定できます。

### 1. まず接続されている画面を確認する

```bash
literary-clock monitors      # または: python3 -m literaryclock monitors
```

```
検出されたディスプレイ: 2 台  (* = プライマリ)

  [0]* HDMI-1       1920x1080+0+0       Dell U2415
  [1]  HDMI-2       3840x2160+1920+0    Sony TV

使い方:
  literary-clock --monitor 1
  literary-clock --monitor HDMI-2
  LITCLOCK_MONITOR=1 literary-clock
```

### 2. 表示先を指定して起動する

次のどの書き方でも同じ画面を選べます。覚えやすいものを使ってください。

```bash
literary-clock --monitor 1          # 一覧の番号 (0 始まり)
literary-clock --monitor HDMI-2     # コネクタ名
literary-clock --monitor right      # 位置 (left / right / top / bottom)
literary-clock --monitor primary    # プライマリ
literary-clock --monitor Sony       # モニタ名の一部 (部分一致)
```

コネクタ名の表記ゆれ (X11 は `HDMI-2`、Wayland は `HDMI-A-2`) は吸収されるので、
`hdmi2` のようなラフな指定でも動きます。

環境変数や設定ファイルでも指定できます。

```bash
LITCLOCK_MONITOR=1 literary-clock
```

```json
{ "monitor": "HDMI-2" }
```

### 3. 自動起動 (systemd) での指定

セットアップ時に渡すのが一番簡単です。

```bash
bash scripts/install_pi.sh --monitor 1
```

すでにインストール済みの場合は、同じコマンドを再実行するか、ユニットを直接編集します。

```bash
# ~/.config/systemd/user/literaryclock.service の
#   Environment=LITCLOCK_MONITOR=
# を
#   Environment=LITCLOCK_MONITOR=1
# に変えてから
systemctl --user daemon-reload
systemctl --user restart literaryclock.service
```

### 指定した画面が見つからないとき

既定では警告を出してプライマリに表示します (モニタを抜いた状態でも時計が止まらないため)。
意図した画面でなければ起動してほしくない場合は `--strict-monitor` を付けてください。

```bash
literary-clock --monitor HDMI-2 --strict-monitor   # 見つからなければエラー終了
```

ディスプレイ自体が検出できない環境では、座標を直接指定できます。

```bash
literary-clock --window-position 1920,0 --window-size 1920,1080
```

> 検出は `xrandr` (X11) → `wlr-randr` / `swaymsg` (Wayland) → `/sys/class/drm` の
> 順に best-effort で行います。Wayland で `wlr-randr` が無い場合は
> `sudo apt install wlr-randr` で入れると正確な配置が取得できます。

## SSH 経由で使う (ディスプレイ指定を確実にする)

SSH でログインしたシェルには GUI の環境変数が無いため、素朴に実装すると
「表示先を指定したのに効かない」という問題が起きます。本ソフトはこれを
2 段構えで解決しています。

1. **GUI セッションの自動引き継ぎ** — Pi 本体で動いているデスクトップを探し、
   その環境変数 (`WAYLAND_DISPLAY` / `DISPLAY` / `XDG_RUNTIME_DIR` など) を
   引き継いでから起動します。
2. **全画面化をブラウザ任せにしない** — ウィンドウマネージャ / コンポジタ側で
   出力を指定して全画面にします。GUI が無い場合は `cage` で DRM に直接描画します。

そのため、SSH からでも普段通りのコマンドで狙った画面に表示できます。

```bash
ssh pi@raspberrypi.local
cd ~/LiteraryClock
python3 -m literaryclock --monitor 1        # そのまま 2 番目の HDMI に出る
```

### まず診断する

うまく映らない場合は `doctor` を実行してください。セッション・ディスプレイ・
不足コマンドをまとめて確認できます。

```bash
python3 -m literaryclock doctor --monitor 1
```

```
=== 文学時計 表示環境の診断 ===

実行環境      : SSH などのリモートシェル
  DISPLAY           = (未設定)
  WAYLAND_DISPLAY   = (未設定)

--- GUI セッション ---
検出した GUI セッション: 1 件  (上が優先)

  → [0] wayland:wayland-0  user=pi  labwc  seat0  via proc
        WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000

採用するセッション: wayland:wayland-0  user=pi  labwc  seat0  via proc

--- ディスプレイ ---
検出されたディスプレイ: 2 台  (* = プライマリ)

  [0]* HDMI-1       1920x1080+0+0       Dell U2415
  [1]  HDMI-2       3840x2160+1920+0    Sony TV

--- 全画面表示バックエンド ---
選択: wlr  (wlroots: 対象以外の出力を一時的に無効化する)
  必要なコマンドは揃っています。
```

### 全画面表示バックエンド

`--display-backend` で全画面化の方法を選べます。既定の `auto` は環境から
自動判別するので、通常は指定不要です。

| バックエンド | 方式 | 必要なもの |
|---|---|---|
| `auto` | 環境から自動選択 (既定) | — |
| `x11` | ウィンドウを目的モニタへ移動してから WM の全画面状態を立てる | `xdotool` |
| `sway` | sway / i3 の IPC で「この出力で全画面」を起動前に予約する | `swaymsg` |
| `wlr` | labwc / wayfire 向け。対象以外の出力を一時的に無効化する (終了時に復元) | `wlr-randr` |
| `cage` | GUI セッション不要。DRM コネクタを直接指定して単独表示する | `cage` |
| `window` | 従来動作 (ブラウザの `--kiosk` 任せ) | — |

> **なぜブラウザの全画面から離れたのか**
> Wayland ではクライアントが自分のウィンドウ位置を決められない仕様のため、
> Chromium の `--window-position` は黙って無視されます。`--kiosk` もコンポジタが
> 選んだ出力 (通常はプライマリ) に出てしまい、表示先を指定できません。
> そこで「WM / コンポジタ側で出力を指定する」方式に切り替えています。

### GUI を使わない構成 (最も確実)

デスクトップを起動せず、`cage` で直接 HDMI に描画する方法です。SSH のみで
運用する場合や Raspberry Pi OS Lite ではこちらが最も確実です。

```bash
sudo apt install -y cage
python3 -m literaryclock --monitor 1 --display-backend cage
```

`cage` は「クライアント 1 つだけを全画面表示する」Wayland コンポジタで、
DRM コネクタ (`HDMI-A-1` / `HDMI-A-2`) を直接指定できます。X11 も labwc も
不要なので、SSH 経由でも表示先が確実に決まります。

自動起動もセットアップできます。

```bash
bash scripts/install_pi.sh --headless --monitor 1
```

これは `systemd/literaryclock-cage.service` をシステムユニットとして配置します。
デスクトップ自動ログインとは画面を取り合うため、
`raspi-config` で **Console Autologin** に変更しておいてください。

### セッションを明示的に選ぶ

複数のセッションがある場合や自動判別がうまくいかない場合は指定できます。

```bash
python3 -m literaryclock --session wayland-0     # Wayland ソケット名
python3 -m literaryclock --session :0            # X11 のディスプレイ番号
python3 -m literaryclock --no-adopt-session      # 引き継ぎを無効化 (従来動作)
```

### `ssh -X` (X11 転送) について

`ssh -X` すると `DISPLAY=localhost:10.0` が設定されますが、これは
**接続元 PC の画面** です。そのまま使うと Pi ではなく手元の PC にブラウザが
出てしまうため、本ソフトは X11 転送の `DISPLAY` を自動的に検出して除外し、
Pi 本体のセッションを優先します。

### SSH 経由で使う場合の注意

- `wlr` バックエンドは表示中だけ他の出力を無効化します。両方の画面を同時に
  使いたい場合は `cage` バックエンドを使うか、`--no-exclusive-output` を
  付けてください (ただし表示先は保証されなくなります)。
- 自動引き継ぎは自分と同じユーザーのプロセスの環境変数を読む方式のため、
  デスクトップと SSH で同じユーザーを使ってください。

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
  cli.py         コマンドラインインタフェース (run / validate / preview / monitors / doctor)
  config.py      設定の読込・マージ (デフォルト → ファイル → 環境変数 → CLI)
  dataset.py     literary_clock.json の読込・検証・時刻引き当て
  server.py      静的配信 + JSON API (http.server ベース、依存なし)
  monitors.py    接続ディスプレイの検出・選択 (xrandr / wlr-randr / swaymsg / sysfs)
  session.py     GUI セッションの探索と環境変数の引き継ぎ (SSH 経由対策)
  kiosk.py       全画面表示バックエンド (x11 / sway / wlr / cage / window)
  display.py     上記の統合・画面消灯無効化・doctor 診断
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

`tests/` には設定・データセット・サーバ (実際に HTTP サーバを起動して検証)・
マルチモニタ検出・GUI セッション探索・全画面表示バックエンドの 293 件の
テストがあります。`scripts/shots.py` は Playwright で各テーマ・組み方向の
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
- **意図しない方のモニタに表示される** — `literary-clock monitors` で番号とコネクタ名を
  確認し、`--monitor 1` のように指定してください。自動起動の場合は
  `bash scripts/install_pi.sh --monitor 1` を再実行すれば反映されます。
  それでも変わらない場合は `literary-clock doctor --monitor 1` で
  どのバックエンドが選ばれているか確認してください。
- **SSH 経由だとディスプレイ指定が効かない** — `literary-clock doctor` を実行し、
  「採用するセッション」が表示されているか確認してください。表示されない場合は
  デスクトップに自動ログインしていないか、SSH とデスクトップでユーザーが
  異なっています。GUI を使わない構成なら `cage` が最も確実です。
  ```bash
  sudo apt install -y cage
  literary-clock --monitor 1 --display-backend cage
  ```
- **`ssh -X` すると手元の PC に表示される** — X11 転送の `DISPLAY` は自動的に
  除外されますが、明示的に避けたい場合は `ssh` に `-X` を付けずに接続してください。
- **`monitors` でディスプレイが検出されない** — SSH 経由でも自動でセッションを
  引き継ぐようになっていますが、検出できない場合は `sudo apt install wlr-randr`
  (Wayland) で検出精度が上がります。GUI が全く無い環境でも `/sys/class/drm` から
  接続済みの HDMI を列挙できます。

## ライセンス

MIT License. 詳細は [LICENSE](LICENSE) を参照してください。
同梱データセットの引用文自体の著作権については、青空文庫の各作品の「図書カード」の
記載に従ってください。
