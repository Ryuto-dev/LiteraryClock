"""狙ったディスプレイに全画面表示するための kiosk バックエンド群.

「ブラウザの全画面 (``--kiosk``)」に頼ると、表示先ディスプレイを選べない
場面が多い。特に:

  - **Wayland (Raspberry Pi OS Bookworm 既定)** では、クライアントが自分の
    ウィンドウ位置を決められない仕様のため ``--window-position`` が黙って
    無視される。``--kiosk`` はコンポジタが選んだ出力 (通常はプライマリ) に
    出てしまう。
  - **SSH 経由** では ``DISPLAY`` / ``WAYLAND_DISPLAY`` が無いか、X11 転送の
    値になっており、ブラウザが起動しない/手元の PC に出てしまう。

そこで本モジュールでは「ウィンドウマネージャ / コンポジタ側で出力を指定して
全画面にする」方式に切り替える。バックエンドは環境に応じて選ばれる:

===============  =========================================================
バックエンド      方式
===============  =========================================================
``x11``          ``--app=`` で普通のウィンドウとして起動し、``xdotool`` /
                 ``wmctrl`` で目的モニタへ移動してから WM の全画面状態
                 (``_NET_WM_STATE_FULLSCREEN``) を立てる。
``sway``         sway / i3 の IPC (``swaymsg for_window``) で、起動前に
                 「このウィンドウは出力 X で全画面」と予約する。
``wlr``          labwc / wayfire など wlroots 系。位置指定 API が無いため、
                 ``wlr-randr`` で対象以外の出力を一時的に無効化して
                 「対象出力しか存在しない」状態を作る (終了時に復元)。
``cage``         GUI セッションが無くても動く。``cage`` (単一クライアント用
                 コンポジタ) を DRM コネクタ直指定で起動し、その唯一の
                 クライアントとしてブラウザを動かす。SSH 経由に最適。
``window``       従来動作 (``--kiosk`` + ``--window-position``)。
===============  =========================================================

いずれも失敗しても時計本体は動くよう best-effort で実装している。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .monitors import Monitor
from .session import GuiSession

log = logging.getLogger("literaryclock.kiosk")

# ブラウザウィンドウを識別するための WM_CLASS / app_id
APP_ID = "literaryclock"

BACKENDS = ("auto", "x11", "sway", "wlr", "cage", "window")

# ウィンドウが現れるまでの最大待ち時間 (秒)
WINDOW_WAIT_TIMEOUT = 20.0
WINDOW_POLL_INTERVAL = 0.25


class KioskError(RuntimeError):
    """kiosk 起動が決定的に失敗した場合に送出される."""


@dataclass
class KioskProcess:
    """起動した kiosk プロセスと、終了時に戻すべき状態."""

    process: subprocess.Popen | None = None
    backend: str = ""
    monitor: Monitor | None = None
    notes: list[str] = field(default_factory=list)
    _cleanups: list[Callable[[], None]] = field(default_factory=list)

    def poll(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def add_cleanup(self, func: Callable[[], None]) -> None:
        self._cleanups.append(func)

    def terminate(self, timeout: float = 3.0) -> None:
        """プロセスを止め、変更したディスプレイ設定を元に戻す."""
        proc = self.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.SubprocessError:  # pragma: no cover - 強制終了
                proc.kill()
        while self._cleanups:
            func = self._cleanups.pop()
            try:
                func()
            except Exception as exc:  # pragma: no cover - 復元は best-effort
                log.debug("後片付けに失敗: %s", exc)


# --------------------------------------------------------------------------
# 外部コマンド
# --------------------------------------------------------------------------
def _run(
    cmd: Sequence[str],
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
    capture: bool = False,
) -> tuple[int, str]:
    """外部コマンドを実行して (終了コード, 標準出力) を返す."""
    if not shutil.which(cmd[0]):
        log.debug("%s が見つかりません", cmd[0])
        return 127, ""
    try:
        proc = subprocess.run(
            list(cmd),
            check=False,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s の実行に失敗: %s", " ".join(cmd), exc)
        return 1, ""
    return proc.returncode, (proc.stdout or "") if capture else ""


def _have(name: str) -> bool:
    return shutil.which(name) is not None


# --------------------------------------------------------------------------
# バックエンドの選択
# --------------------------------------------------------------------------
def choose_backend(
    spec: str,
    session: GuiSession | None,
    monitor: Monitor | None,
) -> str:
    """使用するバックエンドを決める.

    spec が ``auto`` (既定) の場合、セッション種別と利用可能なツールから選ぶ。
    表示先が指定されていない (monitor is None) 場合は、余計な操作をしないよう
    従来の ``window`` バックエンドを使う。
    """
    spec = (spec or "auto").strip().lower()
    if spec not in BACKENDS:
        raise KioskError(
            f"--display-backend は {BACKENDS} のいずれかです: {spec!r}"
        )
    if spec != "auto":
        return spec

    # GUI セッションが全く無い → cage で DRM 直描画 (SSH 経由の主経路)
    if session is None or not session.usable:
        if _have("cage"):
            return "cage"
        return "window"

    if monitor is None:
        # 表示先指定が無いなら、素直に全画面にするだけでよい
        return "window"

    if session.wayland_display:
        if session.env.get("SWAYSOCK") or session.compositor in ("sway", "river"):
            if _have("swaymsg"):
                return "sway"
        if _have("wlr-randr"):
            return "wlr"
        if _have("cage"):
            return "cage"
        return "window"

    if session.display:
        if _have("xdotool") or _have("wmctrl"):
            return "x11"
        return "window"

    return "window"


def describe_backend(backend: str) -> str:
    """バックエンドの説明 (ログ・doctor 表示用)."""
    return {
        "x11": "X11: ウィンドウを移動してから WM の全画面状態を立てる",
        "sway": "sway/i3 IPC: 出力を指定して全画面を予約する",
        "wlr": "wlroots: 対象以外の出力を一時的に無効化する",
        "cage": "cage: DRM コネクタを直接指定して単独コンポジタで表示する",
        "window": "ブラウザの kiosk モード (従来動作)",
    }.get(backend, backend)


def backend_requirements(backend: str) -> list[str]:
    """バックエンドに必要な外部コマンドのうち、不足しているものを返す."""
    needs = {
        "x11": ["xdotool"],
        "sway": ["swaymsg"],
        "wlr": ["wlr-randr"],
        "cage": ["cage"],
        "window": [],
    }.get(backend, [])
    return [name for name in needs if not _have(name)]


# --------------------------------------------------------------------------
# ブラウザコマンドの組み立て
# --------------------------------------------------------------------------
# 描画を軽くしつつ余計な UI を消すフラグ (全画面化の方法とは独立)
BASE_CHROMIUM_FLAGS = (
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


def build_browser_command(
    exe: str,
    url: str,
    backend: str,
    monitor: Monitor | None = None,
    profile_dir: str | None = None,
    window_position: str = "",
    window_size: str = "",
    wayland: bool = False,
) -> list[str]:
    """ブラウザの起動コマンドを組み立てる.

    ``x11`` / ``sway`` バックエンドでは全画面化を WM 側で行うため、
    ブラウザ自身の ``--kiosk`` は付けない (付けるとウィンドウ移動が
    効かなくなる実装が多い)。
    """
    name = Path(exe).name.lower()
    if "firefox" in name:
        cmd = [exe]
        if backend in ("window", "cage", "wlr"):
            cmd.append("--kiosk")
        cmd += ["--class", APP_ID, url]
        return cmd

    profile = profile_dir or str(
        Path(tempfile.gettempdir()) / "literaryclock-chromium-profile"
    )
    Path(profile).mkdir(parents=True, exist_ok=True)

    cmd = [exe, *BASE_CHROMIUM_FLAGS, f"--class={APP_ID}"]

    # WM 側で全画面にするバックエンドでは --kiosk を使わない
    if backend in ("window", "cage", "wlr"):
        cmd += ["--kiosk", "--start-fullscreen"]

    if wayland:
        cmd += ["--ozone-platform=wayland", "--enable-features=UseOzonePlatform"]

    # X11 では起動直後から目的モニタに出したいので、ヒントとして位置を渡す
    # (Wayland では無視されるため付けない)
    if not wayland and backend in ("x11", "window"):
        pos = (window_position or "").strip()
        size = (window_size or "").strip()
        if monitor is not None:
            if not pos:
                pos = monitor.position_arg
            if not size and monitor.has_geometry:
                size = monitor.size_arg
        if pos:
            cmd.append(f"--window-position={pos}")
        if size:
            cmd.append(f"--window-size={size}")

    cmd += [f"--user-data-dir={profile}", f"--app={url}"]
    return cmd


# --------------------------------------------------------------------------
# X11: ウィンドウを移動して WM 全画面にする
# --------------------------------------------------------------------------
def find_window_x11(
    env: dict[str, str],
    pid: int,
    timeout: float = WINDOW_WAIT_TIMEOUT,
) -> str:
    """起動したブラウザのウィンドウ ID を探す (見つからなければ空文字)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for args in (
            ["xdotool", "search", "--sync", "--limit", "1", "--onlyvisible",
             "--class", APP_ID],
            ["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
            ["xdotool", "search", "--class", APP_ID],
        ):
            code, out = _run(args, env=env, timeout=5.0, capture=True)
            if code == 0:
                ids = [line.strip() for line in out.splitlines() if line.strip()]
                if ids:
                    return ids[-1]
        time.sleep(WINDOW_POLL_INTERVAL)
    return ""


def fullscreen_on_monitor_x11(
    env: dict[str, str],
    monitor: Monitor | None,
    pid: int,
) -> bool:
    """X11 上でウィンドウを目的モニタへ移動し、WM 全画面にする."""
    wid = find_window_x11(env, pid)
    if not wid:
        log.warning(
            "ブラウザのウィンドウを特定できませんでした "
            "(xdotool が必要です: sudo apt install xdotool)"
        )
        return False

    ok = True
    if monitor is not None:
        # 全画面状態のままでは移動できないので、いったん解除する
        _run(["xdotool", "windowstate", "--remove", "FULLSCREEN", wid], env=env)
        code, _ = _run(
            ["xdotool", "windowmove", wid, str(monitor.x), str(monitor.y)], env=env
        )
        if code != 0:
            # xdotool が無い/失敗した場合は wmctrl で試す
            geom = (
                f"0,{monitor.x},{monitor.y},"
                f"{monitor.width or -1},{monitor.height or -1}"
            )
            code, _ = _run(["wmctrl", "-i", "-r", wid, "-e", geom], env=env)
            ok = code == 0
        elif monitor.has_geometry:
            _run(
                ["xdotool", "windowsize", wid,
                 str(monitor.width), str(monitor.height)],
                env=env,
            )

    _run(["xdotool", "windowactivate", wid], env=env)
    code, _ = _run(["xdotool", "windowstate", "--add", "FULLSCREEN", wid], env=env)
    if code != 0:
        code, _ = _run(["wmctrl", "-i", "-r", wid, "-b", "add,fullscreen"], env=env)
        ok = ok and code == 0

    if ok and monitor is not None:
        log.info("X11: ウィンドウを %s (%s) で全画面にしました",
                 monitor.name, monitor.geometry)
    return ok


# --------------------------------------------------------------------------
# sway / i3: IPC で出力を指定して全画面を予約する
# --------------------------------------------------------------------------
def sway_output_name(env: dict[str, str], monitor: Monitor) -> str:
    """sway 側での出力名を返す (検出名と同じであればそのまま)."""
    return monitor.name


def reserve_fullscreen_sway(
    env: dict[str, str],
    monitor: Monitor | None,
) -> Callable[[], None] | None:
    """``for_window`` ルールを登録して、出現時に出力指定 + 全画面にする.

    起動前に予約しておくのがポイント。ウィンドウが出た瞬間に sway 側で
    移動・全画面化されるため、ちらつきが無い。

    戻り値はルールを取り消すクリーンアップ関数 (登録できなければ None)。
    """
    if monitor is None:
        code, _ = _run(
            ["swaymsg", f'for_window [app_id="{APP_ID}"] fullscreen enable'], env=env
        )
        return None if code != 0 else lambda: None

    output = sway_output_name(env, monitor)
    rule = (
        f'for_window [app_id="{APP_ID}"] '
        f'move container to output "{output}", fullscreen enable'
    )
    code, _ = _run(["swaymsg", rule], env=env)
    if code != 0:
        # X11 (i3) や XWayland クライアントは class で一致させる
        rule = (
            f'for_window [class="{APP_ID}"] '
            f'move container to output "{output}", fullscreen enable'
        )
        code, _ = _run(["swaymsg", rule], env=env)
    if code != 0:
        log.warning("swaymsg でのルール登録に失敗しました (出力: %s)", output)
        return None

    log.info("sway: 出力 %s に全画面表示を予約しました", output)
    return lambda: None


def move_existing_window_sway(
    env: dict[str, str],
    monitor: Monitor | None,
) -> bool:
    """すでに出ているウィンドウを出力へ移動して全画面にする (保険)."""
    if monitor is None:
        return False
    output = sway_output_name(env, monitor)
    for selector in (f'[app_id="{APP_ID}"]', f'[class="{APP_ID}"]'):
        code, _ = _run(
            ["swaymsg", f'{selector} move container to output "{output}"'], env=env
        )
        if code == 0:
            _run(["swaymsg", f"{selector} fullscreen enable"], env=env)
            return True
    return False


# --------------------------------------------------------------------------
# wlroots (labwc / wayfire): 対象以外の出力を一時的に無効化する
# --------------------------------------------------------------------------
def make_output_exclusive(
    env: dict[str, str],
    monitor: Monitor,
    monitors: list[Monitor],
) -> Callable[[], None] | None:
    """対象出力だけを有効にし、復元用の関数を返す.

    labwc / wayfire にはウィンドウ位置を指定する IPC が無い。しかし
    「表示したい出力しか有効になっていない」状態を作れば、全画面化した
    ウィンドウは必ずその出力に出る。終了時に元の状態へ戻す。
    """
    others = [
        m for m in monitors
        if m.name != monitor.name and m.active and m.has_geometry
    ]
    if not others:
        return None
    if not _have("wlr-randr"):
        log.warning(
            "wlr-randr が無いため出力の切り替えができません "
            "(sudo apt install wlr-randr)"
        )
        return None

    turned_off: list[Monitor] = []
    for other in others:
        code, _ = _run(["wlr-randr", "--output", other.name, "--off"], env=env)
        if code == 0:
            turned_off.append(other)
            log.info("wlroots: 出力 %s を一時的に無効化しました", other.name)
        else:
            log.warning("出力 %s の無効化に失敗しました", other.name)

    # 対象出力は確実に有効化しておく
    _run(["wlr-randr", "--output", monitor.name, "--on"], env=env)
    if not turned_off:
        return None

    def restore() -> None:
        for mon in turned_off:
            args = ["wlr-randr", "--output", mon.name, "--on"]
            if mon.has_geometry:
                args += ["--mode", f"{mon.width}x{mon.height}",
                         "--pos", f"{mon.x},{mon.y}"]
            _run(args, env=env)
            log.info("wlroots: 出力 %s を元に戻しました", mon.name)

    return restore


# --------------------------------------------------------------------------
# cage: GUI セッション不要で DRM コネクタを直接指定する
# --------------------------------------------------------------------------
def build_cage_env(
    monitor: Monitor | None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """``cage`` を DRM バックエンドで動かすための環境変数を作る.

    wlroots は ``WLR_OUTPUTS`` で使用する DRM コネクタを絞り込めるため、
    ここに ``HDMI-A-2`` のようなコネクタ名を渡すことで表示先を確定できる。
    GUI セッションの環境変数は不要なので、SSH 経由でも狙った画面に出せる。
    """
    env = dict(os.environ if base_env is None else base_env)
    # 既存セッションに引きずられないよう、Wayland/X11 の接続情報を落とす
    for key in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
        env.pop(key, None)
    env["WLR_BACKENDS"] = "drm,libinput"
    if monitor is not None:
        env["WLR_OUTPUTS"] = monitor.drm_connector
    env.setdefault("XDG_SESSION_TYPE", "wayland")
    if not env.get("XDG_RUNTIME_DIR"):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        runtime = f"/run/user/{uid}"
        if Path(runtime).is_dir():
            env["XDG_RUNTIME_DIR"] = runtime
        else:  # pragma: no cover - 通常は logind が用意する
            fallback = Path(tempfile.gettempdir()) / f"literaryclock-runtime-{uid}"
            fallback.mkdir(parents=True, exist_ok=True, mode=0o700)
            env["XDG_RUNTIME_DIR"] = str(fallback)
    return env


def build_cage_command(browser_cmd: list[str]) -> list[str]:
    """``cage -s -- <browser ...>`` を組み立てる."""
    return ["cage", "-s", "--", *browser_cmd]


# --------------------------------------------------------------------------
# 統合エントリポイント
# --------------------------------------------------------------------------
def launch(
    url: str,
    browser_exe: str,
    backend: str,
    session: GuiSession | None = None,
    monitor: Monitor | None = None,
    monitors: list[Monitor] | None = None,
    profile_dir: str | None = None,
    window_position: str = "",
    window_size: str = "",
    exclusive_output: bool | None = None,
) -> KioskProcess:
    """選択したバックエンドで kiosk 表示を開始する."""
    monitors = monitors or []
    env = dict(os.environ)
    if session is not None:
        env.update({k: v for k, v in session.env.items() if v})
    wayland = bool(env.get("WAYLAND_DISPLAY")) and not backend == "x11"

    result = KioskProcess(backend=backend, monitor=monitor)

    # --- cage: セッション不要。環境変数を作り直して起動する ---
    if backend == "cage":
        if not _have("cage"):
            raise KioskError(
                "cage が見つかりません。以下でインストールしてください:\n"
                "  sudo apt install -y cage"
            )
        cage_env = build_cage_env(monitor, env)
        browser_cmd = build_browser_command(
            browser_exe, url, backend,
            monitor=monitor,
            profile_dir=profile_dir,
            window_position=window_position,
            window_size=window_size,
            wayland=True,
        )
        cmd = build_cage_command(browser_cmd)
        if monitor is not None:
            log.info(
                "cage: DRM コネクタ %s に単独コンポジタで表示します",
                monitor.drm_connector,
            )
            result.notes.append(f"WLR_OUTPUTS={monitor.drm_connector}")
        result.process = _spawn(cmd, cage_env)
        return result

    # --- sway: 起動前にルールを予約する ---
    reserve_cleanup: Callable[[], None] | None = None
    if backend == "sway":
        reserve_cleanup = reserve_fullscreen_sway(env, monitor)

    # --- wlr: 対象以外の出力を一時的に無効化する ---
    if backend == "wlr" and monitor is not None:
        want_exclusive = True if exclusive_output is None else exclusive_output
        if want_exclusive:
            restore = make_output_exclusive(env, monitor, monitors)
            if restore is not None:
                result.add_cleanup(restore)
                result.notes.append("対象以外の出力を一時的に無効化")
        else:
            log.warning(
                "labwc / wayfire ではウィンドウ位置を指定できないため、"
                "--exclusive-output を付けないと表示先が保証されません。"
            )

    browser_cmd = build_browser_command(
        browser_exe, url, backend,
        monitor=monitor,
        profile_dir=profile_dir,
        window_position=window_position,
        window_size=window_size,
        wayland=wayland,
    )
    result.process = _spawn(browser_cmd, env)
    if reserve_cleanup is not None:
        result.add_cleanup(reserve_cleanup)

    # --- 起動後の後処理 ---
    if backend == "x11":
        pid = result.process.pid if result.process else 0
        if not fullscreen_on_monitor_x11(env, monitor, pid):
            result.notes.append("WM 全画面化に失敗 (ブラウザ側の表示のまま)")
    elif backend == "sway":
        # ルール登録が間に合わなかった場合の保険
        time.sleep(1.0)
        move_existing_window_sway(env, monitor)

    return result


def _spawn(cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
    """プロセスを起動する (標準出力は捨てる)."""
    log.debug("起動コマンド: %s", " ".join(cmd))
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise KioskError(f"起動に失敗しました ({cmd[0]}): {exc}") from exc
