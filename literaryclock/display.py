"""ブラウザ kiosk 起動と Raspberry Pi 向けディスプレイ制御.

Raspberry Pi OS (Bookworm 以降は Wayland/labwc, 以前は X11) の双方を想定し、
利用可能なコマンドのみを best-effort で実行する。失敗しても時計本体は動作する。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .monitors import Monitor, MonitorError, detect_monitors, format_table, resolve

log = logging.getLogger("literaryclock.display")

# Raspberry Pi OS / 一般的な Linux で見つかるブラウザ候補
BROWSER_CANDIDATES = (
    "chromium-browser",  # Raspberry Pi OS の標準
    "chromium",
    "google-chrome-stable",
    "google-chrome",
    "chrome",
    "microsoft-edge",
    "firefox",
)

# Pi の GPU/メモリが限られるため、描画を軽くするフラグを付与する
CHROMIUM_FLAGS = (
    "--kiosk",
    "--start-fullscreen",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI,Translate",
    "--disable-translate",
    "--disable-pinch",
    "--overscroll-history-navigation=0",
    "--no-first-run",
    "--fast-start",
    "--disable-component-update",
    "--check-for-update-interval=31536000",
    "--autoplay-policy=no-user-gesture-required",
    "--hide-scrollbars",
    "--password-store=basic",
    "--disable-notifications",
)


def find_browser(preferred: str = "") -> str | None:
    """利用可能なブラウザの実行パスを返す."""
    if preferred:
        resolved = shutil.which(preferred) or (
            preferred if Path(preferred).is_file() else None
        )
        if resolved:
            return resolved
        log.warning("指定されたブラウザが見つかりません: %s (自動検出に切替)", preferred)

    for name in BROWSER_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def has_display() -> bool:
    """GUI セッション (X11 / Wayland) が利用可能か."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run(cmd: list[str], desc: str) -> bool:
    """外部コマンドを best-effort で実行する."""
    if not shutil.which(cmd[0]):
        log.debug("%s: %s が無いのでスキップ", desc, cmd[0])
        return False
    try:
        subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        log.debug("%s: %s", desc, " ".join(cmd))
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s に失敗: %s", desc, exc)
        return False


def disable_screen_blanking() -> None:
    """スクリーンセーバ / DPMS による画面消灯を無効化する (X11)."""
    if not os.environ.get("DISPLAY"):
        log.debug("X11 セッションではないため画面消灯設定をスキップ")
        return
    _run(["xset", "s", "off"], "スクリーンセーバ無効化")
    _run(["xset", "s", "noblank"], "ブランク無効化")
    _run(["xset", "-dpms"], "DPMS 無効化")


def hide_cursor() -> subprocess.Popen | None:
    """マウスカーソルを隠す (unclutter を常駐させる)."""
    if not os.environ.get("DISPLAY"):
        return None
    if not shutil.which("unclutter"):
        log.debug("unclutter が無いためカーソル非表示をスキップ")
        return None
    try:
        proc = subprocess.Popen(
            ["unclutter", "-idle", "0.1", "-root"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.debug("unclutter を起動しました (pid=%s)", proc.pid)
        return proc
    except OSError as exc:
        log.debug("unclutter の起動に失敗: %s", exc)
        return None


def select_monitor(
    spec: str = "",
    fallback: bool = True,
) -> Monitor | None:
    """``--monitor`` の指定から表示先モニタを決める.

    - spec が空 → None (OS 任せ = 通常はプライマリ)
    - 見つからない場合、fallback=True なら警告を出してプライマリへ、
      fallback=False なら :class:`MonitorError` をそのまま送出する。
    """
    spec = (spec or "").strip()
    if not spec:
        return None

    monitors = detect_monitors()
    try:
        chosen = resolve(spec, monitors)
    except MonitorError as exc:
        if not fallback:
            raise
        log.warning("%s", exc)
        log.warning("→ プライマリディスプレイで起動します。")
        return None

    if chosen is not None:
        if not chosen.active:
            log.warning(
                "ディスプレイ %s は現在無効 (未使用) です。表示されない可能性があります。",
                chosen.name,
            )
        log.info(
            "表示先ディスプレイ: [%d] %s (%s)",
            chosen.index, chosen.name, chosen.geometry,
        )
    return chosen


def list_monitors_text() -> str:
    """検出されたディスプレイの一覧文字列を返す."""
    return format_table(detect_monitors())


def _window_flags(
    monitor: Monitor | None,
    window_position: str = "",
    window_size: str = "",
) -> list[str]:
    """Chromium に渡すウィンドウ配置フラグを組み立てる.

    優先順位: 明示の window_position/window_size
                → --monitor で選んだモニタの座標
                → 環境変数 LITCLOCK_WINDOW_POSITION / LITCLOCK_WINDOW_SIZE
    """
    pos = (window_position or "").strip() or os.environ.get("LITCLOCK_WINDOW_POSITION", "").strip()
    size = (window_size or "").strip() or os.environ.get("LITCLOCK_WINDOW_SIZE", "").strip()

    if monitor is not None:
        if not (window_position or "").strip():
            pos = monitor.position_arg
        if not (window_size or "").strip() and monitor.width and monitor.height:
            size = monitor.size_arg

    flags: list[str] = []
    if pos:
        flags.append(f"--window-position={pos}")
    if size:
        flags.append(f"--window-size={size}")
    return flags


def launch_kiosk(
    url: str,
    browser: str = "",
    profile_dir: str | None = None,
    monitor: Monitor | None = None,
    window_position: str = "",
    window_size: str = "",
) -> subprocess.Popen | None:
    """ブラウザを kiosk (全画面) モードで起動する.

    monitor を指定すると、そのディスプレイの座標にウィンドウを配置して
    全画面化する (Raspberry Pi の HDMI 2 口同時接続対応)。

    成功時は Popen を返す。ブラウザが無い/GUI が無い場合は None。
    """
    exe = find_browser(browser)
    if not exe:
        log.error(
            "ブラウザが見つかりません。chromium をインストールしてください:\n"
            "  sudo apt install -y chromium-browser\n"
            "  (または --no-kiosk で手動アクセス: %s)",
            url,
        )
        return None

    if not has_display():
        log.error(
            "GUI セッションが検出できません (DISPLAY/WAYLAND_DISPLAY が未設定)。\n"
            "  デスクトップ環境から実行するか、--no-kiosk でサーバのみ起動してください: %s",
            url,
        )
        return None

    name = Path(exe).name
    cmd = [exe]
    flags = _window_flags(monitor, window_position, window_size)

    if "firefox" in name:
        # Firefox には --window-position が無いので、ウィンドウマネージャ側で
        # 移動させる (X11 のみ, best-effort)。
        cmd += ["--kiosk", url]
        if monitor is not None:
            log.info(
                "Firefox はモニタ指定に限定対応です。起動後に %s へ移動を試みます。",
                monitor.name,
            )
    else:
        profile = profile_dir or str(
            Path(tempfile.gettempdir()) / "literaryclock-chromium-profile"
        )
        Path(profile).mkdir(parents=True, exist_ok=True)
        cmd += list(CHROMIUM_FLAGS)
        # Raspberry Pi OS Bookworm 以降は既定が Wayland (labwc)。
        # DISPLAY が無く WAYLAND_DISPLAY のみの場合は明示的に ozone platform を
        # 指定しないと、XWayland 経由の起動に失敗する/表示がぼやけることがある。
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            cmd += ["--ozone-platform=wayland", "--enable-features=UseOzonePlatform"]
        # 複数ディスプレイ環境向けのウィンドウ配置 (X11 / Wayland 共通)
        cmd += flags
        cmd += [f"--user-data-dir={profile}", "--app=" + url]

    log.info("ブラウザを起動します: %s", name)
    if flags:
        log.debug("ウィンドウ配置: %s", " ".join(flags))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        log.error("ブラウザの起動に失敗しました: %s", exc)
        return None

    if monitor is not None and "firefox" in name:
        move_window_to_monitor(monitor)
    return proc


def move_window_to_monitor(monitor: Monitor, title_hint: str = "") -> bool:
    """X11 上で既存ウィンドウを指定モニタへ移動する (best-effort).

    Chromium は --window-position で指定できるため主に Firefox 向け。
    wmctrl / xdotool が無ければ何もせず False を返す。
    """
    if not os.environ.get("DISPLAY"):
        log.debug("X11 ではないためウィンドウ移動をスキップ")
        return False
    geom = f"0,{monitor.x},{monitor.y},{monitor.width or -1},{monitor.height or -1}"
    pattern = title_hint or ":ACTIVE:"
    return _run(["wmctrl", "-r", pattern, "-e", geom], "ウィンドウをモニタへ移動")
