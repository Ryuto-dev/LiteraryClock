"""ブラウザ kiosk 起動と Raspberry Pi 向けディスプレイ制御.

Raspberry Pi OS (Bookworm 以降は Wayland/labwc, 以前は X11) の双方を想定し、
利用可能なコマンドのみを best-effort で実行する。失敗しても時計本体は動作する。

SSH 経由での運用では、ログインシェルに GUI セッションの環境変数が無いため、
:mod:`literaryclock.session` で本体のセッションを探して引き継いだうえで、
:mod:`literaryclock.kiosk` のバックエンド (WM/コンポジタ側での全画面化) を
使って表示先ディスプレイを確定させる。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .kiosk import (
    BACKENDS,
    KioskError,
    KioskProcess,
    backend_requirements,
    choose_backend,
    describe_backend,
)
from .kiosk import launch as _kiosk_launch
from .monitors import Monitor, MonitorError, detect_monitors, format_table, resolve
from .session import GuiSession, ensure_gui_session, format_sessions, is_remote_shell

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

# 後方互換のために残している (実際のフラグ組み立ては kiosk.py 側)
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


def _run(cmd: list[str], desc: str, env: dict[str, str] | None = None) -> bool:
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
            env=env,
        )
        log.debug("%s: %s", desc, " ".join(cmd))
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s に失敗: %s", desc, exc)
        return False


def disable_screen_blanking(env: dict[str, str] | None = None) -> None:
    """スクリーンセーバ / DPMS による画面消灯を無効化する.

    X11 では ``xset``、wlroots 系では ``wlr-randr`` で出力を起こす。
    env に GUI セッションの環境変数を渡せば SSH 経由でも効く。
    """
    environ = dict(os.environ) if env is None else dict(env)
    if environ.get("DISPLAY"):
        _run(["xset", "s", "off"], "スクリーンセーバ無効化", environ)
        _run(["xset", "s", "noblank"], "ブランク無効化", environ)
        _run(["xset", "-dpms"], "DPMS 無効化", environ)
        return
    if environ.get("WAYLAND_DISPLAY"):
        # labwc / wayfire では画面消灯は idle デーモン側の設定になるため、
        # ここでは出力が確実に on であることだけ担保する。
        _run(["wlr-randr"], "出力状態の確認", environ)
        log.debug(
            "Wayland では画面消灯の抑止をコンポジタ側で設定してください "
            "(例: wlopm / labwc の idle 設定)"
        )
        return
    log.debug("GUI セッションが無いため画面消灯設定をスキップ")


def hide_cursor(env: dict[str, str] | None = None) -> subprocess.Popen | None:
    """マウスカーソルを隠す (unclutter を常駐させる)."""
    environ = dict(os.environ) if env is None else dict(env)
    if not environ.get("DISPLAY"):
        return None
    if not shutil.which("unclutter"):
        log.debug("unclutter が無いためカーソル非表示をスキップ")
        return None
    try:
        proc = subprocess.Popen(
            ["unclutter", "-idle", "0.1", "-root"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environ,
        )
        log.debug("unclutter を起動しました (pid=%s)", proc.pid)
        return proc
    except OSError as exc:
        log.debug("unclutter の起動に失敗: %s", exc)
        return None


def prepare_session(
    session_spec: str = "",
    adopt_session: bool = True,
) -> GuiSession | None:
    """GUI セッションを探して環境変数を引き継ぐ.

    SSH 経由でもデスクトップ側の ``DISPLAY`` / ``WAYLAND_DISPLAY`` を
    掴めるようにするための前処理。
    """
    session, applied = ensure_gui_session(session_spec, enabled=adopt_session)
    if session is None and is_remote_shell():
        log.info(
            "GUI セッションが見つかりませんでした。"
            "cage バックエンド (DRM 直描画) での表示を試みます。"
        )
    return session


def session_env(session: GuiSession | None) -> dict[str, str]:
    """セッションの環境変数を現在の環境にマージした辞書を返す."""
    env = dict(os.environ)
    if session is not None:
        env.update({k: v for k, v in session.env.items() if v})
    return env


def select_monitor(
    spec: str = "",
    fallback: bool = True,
    env: dict[str, str] | None = None,
    monitors: list[Monitor] | None = None,
) -> Monitor | None:
    """``--monitor`` の指定から表示先モニタを決める.

    - spec が空 → None (OS 任せ = 通常はプライマリ)
    - 見つからない場合、fallback=True なら警告を出してプライマリへ、
      fallback=False なら :class:`MonitorError` をそのまま送出する。
    """
    spec = (spec or "").strip()
    if not spec:
        return None

    if monitors is None:
        monitors = detect_monitors(env=env)
    try:
        chosen = resolve(spec, monitors, env=env)
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


def list_monitors_text(env: dict[str, str] | None = None) -> str:
    """検出されたディスプレイの一覧文字列を返す."""
    return format_table(detect_monitors(env=env))


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
    pos = (window_position or "").strip() or os.environ.get(
        "LITCLOCK_WINDOW_POSITION", ""
    ).strip()
    size = (window_size or "").strip() or os.environ.get(
        "LITCLOCK_WINDOW_SIZE", ""
    ).strip()

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
    session: GuiSession | None = None,
    backend: str = "auto",
    monitors: list[Monitor] | None = None,
    exclusive_output: bool | None = None,
) -> KioskProcess | None:
    """ブラウザを全画面 (kiosk) で起動する.

    ``backend`` で全画面化の方法を選べる:

      - ``auto``   環境から自動選択 (既定)
      - ``x11``    ウィンドウを移動して WM 全画面 (``_NET_WM_STATE_FULLSCREEN``)
      - ``sway``   sway/i3 IPC で出力を指定
      - ``wlr``    labwc/wayfire: 対象以外の出力を一時無効化
      - ``cage``   GUI セッション不要。DRM コネクタを直接指定
      - ``window`` 従来動作 (ブラウザの ``--kiosk``)

    成功時は :class:`~literaryclock.kiosk.KioskProcess` を返す。
    ブラウザが無い場合や決定的に失敗した場合は None。
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

    try:
        chosen_backend = choose_backend(backend, session, monitor)
    except KioskError as exc:
        log.error("%s", exc)
        return None

    missing = backend_requirements(chosen_backend)
    if missing:
        log.warning(
            "%s バックエンドには %s が必要です: sudo apt install -y %s",
            chosen_backend, " / ".join(missing), " ".join(missing),
        )

    if chosen_backend != "cage" and (session is None or not session.usable):
        log.error(
            "GUI セッションが検出できません (DISPLAY/WAYLAND_DISPLAY が未設定)。\n"
            "  SSH 経由の場合は cage を入れると GUI 無しで表示できます:\n"
            "    sudo apt install -y cage\n"
            "    literary-clock --monitor 1 --display-backend cage\n"
            "  サーバのみ起動する場合は --no-kiosk を使ってください: %s",
            url,
        )
        return None

    log.info(
        "全画面表示バックエンド: %s (%s)",
        chosen_backend, describe_backend(chosen_backend),
    )
    log.info("ブラウザを起動します: %s", Path(exe).name)

    try:
        result = _kiosk_launch(
            url,
            exe,
            chosen_backend,
            session=session,
            monitor=monitor,
            monitors=monitors or [],
            profile_dir=profile_dir,
            window_position=window_position,
            window_size=window_size,
            exclusive_output=exclusive_output,
        )
    except KioskError as exc:
        log.error("kiosk 起動に失敗しました: %s", exc)
        return None

    for note in result.notes:
        log.debug("補足: %s", note)
    return result


def move_window_to_monitor(monitor: Monitor, title_hint: str = "") -> bool:
    """X11 上で既存ウィンドウを指定モニタへ移動する (best-effort).

    後方互換のために残している。通常は ``x11`` バックエンドが
    ``xdotool`` を用いて移動と全画面化をまとめて行う。
    """
    if not os.environ.get("DISPLAY"):
        log.debug("X11 ではないためウィンドウ移動をスキップ")
        return False
    geom = f"0,{monitor.x},{monitor.y},{monitor.width or -1},{monitor.height or -1}"
    pattern = title_hint or ":ACTIVE:"
    return _run(["wmctrl", "-r", pattern, "-e", geom], "ウィンドウをモニタへ移動")


def doctor_text(
    session_spec: str = "",
    monitor_spec: str = "",
    backend_spec: str = "auto",
) -> str:
    """``literary-clock doctor`` 用の診断レポートを組み立てる.

    SSH 経由で表示できない原因を切り分けられるよう、
    セッション・ディスプレイ・必要コマンドの状況をまとめて出す。
    """
    lines: list[str] = ["=== 文学時計 表示環境の診断 ===", ""]

    remote = is_remote_shell()
    lines.append(f"実行環境      : {'SSH などのリモートシェル' if remote else 'ローカル端末'}")
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE"):
        lines.append(f"  {key:<18}= {os.environ.get(key, '(未設定)')}")
    lines.append("")

    # --- GUI セッション ---
    lines.append("--- GUI セッション ---")
    lines.append(format_sessions())
    lines.append("")

    session = prepare_session(session_spec, adopt_session=True)
    if session is not None and session.usable:
        lines.append(f"採用するセッション: {session.label()}")
        lines.append(f"  {session.env_summary()}")
    else:
        lines.append("採用するセッション: なし (GUI セッションを掴めませんでした)")
        lines.append("  → cage をインストールすると GUI 無しでも表示できます:")
        lines.append("     sudo apt install -y cage")
    lines.append("")

    # --- ディスプレイ ---
    env = session_env(session)
    monitors = detect_monitors(env=env)
    lines.append("--- ディスプレイ ---")
    lines.append(format_table(monitors))
    lines.append("")

    monitor = None
    if monitor_spec:
        try:
            monitor = resolve(monitor_spec, monitors, env=env)
        except MonitorError as exc:
            lines.append(f"--monitor {monitor_spec!r} の解決に失敗:\n{exc}")
        if monitor is not None:
            lines.append(
                f"--monitor {monitor_spec!r} → [{monitor.index}] {monitor.name} "
                f"({monitor.geometry})  DRM: {monitor.drm_connector}"
            )
            lines.append("")

    # --- バックエンド ---
    lines.append("--- 全画面表示バックエンド ---")
    try:
        backend = choose_backend(backend_spec, session, monitor)
    except KioskError as exc:
        lines.append(str(exc))
        backend = "window"
    lines.append(f"選択: {backend}  ({describe_backend(backend)})")
    missing = backend_requirements(backend)
    if missing:
        lines.append(f"  不足コマンド: {' '.join(missing)}")
        lines.append(f"  → sudo apt install -y {' '.join(missing)}")
    else:
        lines.append("  必要なコマンドは揃っています。")
    lines.append("")

    lines.append("--- 関連コマンドの有無 ---")
    for name in ("chromium-browser", "chromium", "cage", "wlr-randr", "swaymsg",
                 "xdotool", "wmctrl", "xrandr", "unclutter"):
        mark = "○" if shutil.which(name) else "×"
        lines.append(f"  {mark} {name}")
    lines.append("")

    if monitor is None and monitor_spec:
        lines.append("ヒント: 表示先が解決できていません。")
        lines.append("  literary-clock monitors で番号とコネクタ名を確認してください。")
    elif backend == "window" and monitor is not None:
        lines.append(
            "ヒント: window バックエンドでは Wayland 上で表示先が保証されません。"
        )
        lines.append("  --display-backend cage または wlr の利用を検討してください。")
    else:
        lines.append("この設定で起動する:")
        cmd = "literary-clock"
        if monitor_spec:
            cmd += f" --monitor {monitor_spec}"
        if backend_spec != "auto":
            cmd += f" --display-backend {backend_spec}"
        lines.append(f"  {cmd}")

    return "\n".join(lines)


__all__ = [
    "BACKENDS",
    "BROWSER_CANDIDATES",
    "CHROMIUM_FLAGS",
    "KioskProcess",
    "disable_screen_blanking",
    "doctor_text",
    "find_browser",
    "has_display",
    "hide_cursor",
    "launch_kiosk",
    "list_monitors_text",
    "move_window_to_monitor",
    "prepare_session",
    "select_monitor",
    "session_env",
]
