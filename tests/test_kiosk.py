"""kiosk モジュール (全画面表示バックエンド) のテスト."""

from __future__ import annotations

import pytest

from literaryclock.kiosk import (
    APP_ID,
    BACKENDS,
    KioskError,
    KioskProcess,
    backend_requirements,
    build_browser_command,
    build_cage_command,
    build_cage_env,
    choose_backend,
    describe_backend,
    make_output_exclusive,
    reserve_fullscreen_sway,
)
from literaryclock.monitors import Monitor, parse_xrandr
from literaryclock.session import GuiSession

CHROMIUM = "/usr/bin/chromium-browser"
URL = "http://127.0.0.1:8730/"

HDMI1 = Monitor(index=0, name="HDMI-1", width=1920, height=1080, x=0, y=0,
                primary=True, connector="card1-HDMI-A-1")
HDMI2 = Monitor(index=1, name="HDMI-2", width=1920, height=1080, x=1920, y=0,
                connector="card1-HDMI-A-2")

WAYLAND_SESSION = GuiSession(
    env={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
    session_type="wayland",
    compositor="labwc",
)
SWAY_SESSION = GuiSession(
    env={
        "WAYLAND_DISPLAY": "wayland-1",
        "SWAYSOCK": "/run/user/1000/sway-ipc.sock",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    },
    session_type="wayland",
    compositor="sway",
)
X11_SESSION = GuiSession(env={"DISPLAY": ":0"}, session_type="x11", compositor="xorg")


def _tools(monkeypatch, available: set[str]):
    """shutil.which をモックして、指定コマンドだけ存在させる."""
    monkeypatch.setattr(
        "literaryclock.kiosk.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )


# --------------------------------------------------------------------------
# バックエンドの選択
# --------------------------------------------------------------------------
def test_choose_backend_rejects_unknown():
    with pytest.raises(KioskError):
        choose_backend("teleport", WAYLAND_SESSION, HDMI2)


def test_choose_backend_explicit_is_respected(monkeypatch):
    _tools(monkeypatch, set())
    for name in BACKENDS:
        if name == "auto":
            continue
        assert choose_backend(name, WAYLAND_SESSION, HDMI2) == name


def test_choose_backend_no_session_prefers_cage(monkeypatch):
    """SSH 経由で GUI セッションが無い場合は cage (DRM 直描画)."""
    _tools(monkeypatch, {"cage", "chromium-browser"})
    assert choose_backend("auto", None, HDMI2) == "cage"


def test_choose_backend_no_session_without_cage_falls_back(monkeypatch):
    _tools(monkeypatch, {"chromium-browser"})
    assert choose_backend("auto", None, HDMI2) == "window"


def test_choose_backend_remote_session_is_not_usable(monkeypatch):
    """ssh -X の転送セッションは使えないので cage に落ちる."""
    _tools(monkeypatch, {"cage"})
    remote = GuiSession(env={"DISPLAY": "localhost:10.0"}, remote=True)
    assert choose_backend("auto", remote, HDMI2) == "cage"


def test_choose_backend_wayland_uses_wlr(monkeypatch):
    _tools(monkeypatch, {"wlr-randr"})
    assert choose_backend("auto", WAYLAND_SESSION, HDMI2) == "wlr"


def test_choose_backend_sway_uses_ipc(monkeypatch):
    _tools(monkeypatch, {"swaymsg", "wlr-randr"})
    assert choose_backend("auto", SWAY_SESSION, HDMI2) == "sway"


def test_choose_backend_x11_uses_xdotool(monkeypatch):
    _tools(monkeypatch, {"xdotool"})
    assert choose_backend("auto", X11_SESSION, HDMI2) == "x11"


def test_choose_backend_x11_without_tools(monkeypatch):
    _tools(monkeypatch, set())
    assert choose_backend("auto", X11_SESSION, HDMI2) == "window"


def test_choose_backend_without_monitor_uses_window(monkeypatch):
    """表示先指定が無ければ余計なことをしない."""
    _tools(monkeypatch, {"wlr-randr", "swaymsg", "xdotool"})
    assert choose_backend("auto", WAYLAND_SESSION, None) == "window"
    assert choose_backend("auto", X11_SESSION, None) == "window"


def test_describe_backend_all_documented():
    for name in BACKENDS:
        if name == "auto":
            continue
        assert describe_backend(name) != name


def test_backend_requirements(monkeypatch):
    _tools(monkeypatch, set())
    assert backend_requirements("cage") == ["cage"]
    assert backend_requirements("wlr") == ["wlr-randr"]
    assert backend_requirements("sway") == ["swaymsg"]
    assert backend_requirements("x11") == ["xdotool"]
    assert backend_requirements("window") == []


def test_backend_requirements_satisfied(monkeypatch):
    _tools(monkeypatch, {"cage", "wlr-randr", "swaymsg", "xdotool"})
    for name in ("cage", "wlr", "sway", "x11", "window"):
        assert backend_requirements(name) == []


# --------------------------------------------------------------------------
# ブラウザコマンドの組み立て
# --------------------------------------------------------------------------
def test_browser_command_window_backend_uses_kiosk(tmp_path):
    cmd = build_browser_command(
        CHROMIUM, URL, "window", monitor=HDMI2, profile_dir=str(tmp_path)
    )
    assert "--kiosk" in cmd
    assert f"--app={URL}" in cmd
    assert "--window-position=1920,0" in cmd
    assert "--window-size=1920,1080" in cmd


def test_browser_command_x11_backend_omits_kiosk(tmp_path):
    """x11 バックエンドでは WM 側で全画面にするため --kiosk を付けない.

    --kiosk のままだとウィンドウを別モニタへ移動できない実装が多い。
    """
    cmd = build_browser_command(
        CHROMIUM, URL, "x11", monitor=HDMI2, profile_dir=str(tmp_path)
    )
    assert "--kiosk" not in cmd
    # 起動直後から目的モニタに出したいので位置ヒントは渡す
    assert "--window-position=1920,0" in cmd


def test_browser_command_sway_backend_omits_kiosk(tmp_path):
    cmd = build_browser_command(
        CHROMIUM, URL, "sway", monitor=HDMI2,
        profile_dir=str(tmp_path), wayland=True,
    )
    assert "--kiosk" not in cmd
    assert "--ozone-platform=wayland" in cmd


def test_browser_command_sets_app_id(tmp_path):
    """WM から識別できるよう app_id/WM_CLASS を固定する."""
    cmd = build_browser_command(
        CHROMIUM, URL, "x11", profile_dir=str(tmp_path)
    )
    assert f"--class={APP_ID}" in cmd


def test_browser_command_wayland_skips_window_position(tmp_path):
    """Wayland ではクライアントが位置を決められないのでフラグを渡さない."""
    cmd = build_browser_command(
        CHROMIUM, URL, "wlr", monitor=HDMI2,
        profile_dir=str(tmp_path), wayland=True,
    )
    assert not any(c.startswith("--window-position") for c in cmd)
    assert "--ozone-platform=wayland" in cmd


def test_browser_command_explicit_position_wins(tmp_path):
    cmd = build_browser_command(
        CHROMIUM, URL, "window", monitor=HDMI2,
        profile_dir=str(tmp_path),
        window_position="10,20", window_size="640,480",
    )
    assert "--window-position=10,20" in cmd
    assert "--window-size=640,480" in cmd


def test_browser_command_monitor_without_size(tmp_path):
    mon = Monitor(index=0, name="HDMI-1", x=1920, y=0)
    cmd = build_browser_command(
        CHROMIUM, URL, "window", monitor=mon, profile_dir=str(tmp_path)
    )
    assert "--window-position=1920,0" in cmd
    assert not any(c.startswith("--window-size") for c in cmd)


def test_browser_command_firefox(tmp_path):
    cmd = build_browser_command("/usr/bin/firefox", URL, "window")
    assert cmd[0] == "/usr/bin/firefox"
    assert "--kiosk" in cmd
    assert URL in cmd


def test_browser_command_firefox_x11_backend_omits_kiosk():
    cmd = build_browser_command("/usr/bin/firefox", URL, "x11")
    assert "--kiosk" not in cmd


def test_browser_command_creates_profile_dir(tmp_path):
    profile = tmp_path / "profile"
    build_browser_command(CHROMIUM, URL, "window", profile_dir=str(profile))
    assert profile.is_dir()


# --------------------------------------------------------------------------
# cage (GUI セッション不要 / SSH 経由の主経路)
# --------------------------------------------------------------------------
def test_cage_env_targets_drm_connector():
    env = build_cage_env(HDMI2, {"XDG_RUNTIME_DIR": "/run/user/1000"})
    assert env["WLR_BACKENDS"] == "drm,libinput"
    # X11 の HDMI-2 ではなく DRM 表記のコネクタ名を使う
    assert env["WLR_OUTPUTS"] == "HDMI-A-2"


def test_cage_env_drops_inherited_session():
    """既存セッションに引きずられないよう接続情報を落とす."""
    env = build_cage_env(
        HDMI1,
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": "localhost:10.0",
            "XAUTHORITY": "/home/pi/.Xauthority",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
    )
    assert "WAYLAND_DISPLAY" not in env
    assert "DISPLAY" not in env
    assert "XAUTHORITY" not in env


def test_cage_env_without_monitor_has_no_output_filter():
    env = build_cage_env(None, {"XDG_RUNTIME_DIR": "/run/user/1000"})
    assert "WLR_OUTPUTS" not in env


def test_cage_env_maps_xrandr_name_to_drm():
    """xrandr 由来 (HDMI-2, connector 未設定) でも DRM 名に変換する."""
    mon = parse_xrandr(
        "HDMI-2 connected 1920x1080+1920+0 (normal left inverted)\n"
    )[0]
    assert build_cage_env(mon, {})["WLR_OUTPUTS"] == "HDMI-A-2"


def test_cage_command_wraps_browser():
    browser = [CHROMIUM, "--kiosk", URL]
    assert build_cage_command(browser) == ["cage", "-s", "--", CHROMIUM, "--kiosk", URL]


# --------------------------------------------------------------------------
# sway バックエンド
# --------------------------------------------------------------------------
def test_reserve_fullscreen_sway_registers_rule(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, env=None, timeout=10.0, capture=False):
        calls.append(list(cmd))
        return 0, ""

    monkeypatch.setattr("literaryclock.kiosk._run", fake_run)
    assert reserve_fullscreen_sway({}, HDMI2) is not None
    rule = calls[0][1]
    assert f'app_id="{APP_ID}"' in rule
    assert 'move container to output "HDMI-2"' in rule
    assert "fullscreen enable" in rule


def test_reserve_fullscreen_sway_falls_back_to_class(monkeypatch):
    """app_id で失敗したら X11 クライアント向けに class で再試行する."""
    calls: list[str] = []

    def fake_run(cmd, env=None, timeout=10.0, capture=False):
        calls.append(cmd[1])
        return (1, "") if 'app_id' in cmd[1] else (0, "")

    monkeypatch.setattr("literaryclock.kiosk._run", fake_run)
    assert reserve_fullscreen_sway({}, HDMI2) is not None
    assert len(calls) == 2
    assert f'class="{APP_ID}"' in calls[1]


def test_reserve_fullscreen_sway_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        "literaryclock.kiosk._run",
        lambda cmd, env=None, timeout=10.0, capture=False: (1, ""),
    )
    assert reserve_fullscreen_sway({}, HDMI2) is None


def test_reserve_fullscreen_sway_without_monitor(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "literaryclock.kiosk._run",
        lambda cmd, env=None, timeout=10.0, capture=False: (
            calls.append(list(cmd)) or (0, "")
        ),
    )
    reserve_fullscreen_sway({}, None)
    assert "move container" not in calls[0][1]
    assert "fullscreen enable" in calls[0][1]


# --------------------------------------------------------------------------
# wlroots バックエンド (出力の排他化)
# --------------------------------------------------------------------------
def test_make_output_exclusive_disables_others(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, env=None, timeout=10.0, capture=False):
        calls.append(list(cmd))
        return 0, ""

    _tools(monkeypatch, {"wlr-randr"})
    monkeypatch.setattr("literaryclock.kiosk._run", fake_run)

    restore = make_output_exclusive({}, HDMI2, [HDMI1, HDMI2])
    assert restore is not None
    # 対象以外 (HDMI-1) を off にする
    assert ["wlr-randr", "--output", "HDMI-1", "--off"] in calls
    # 対象は on にする
    assert ["wlr-randr", "--output", "HDMI-2", "--on"] in calls

    calls.clear()
    restore()
    # 終了時に元へ戻す (モードと位置も復元)
    assert calls[0][:4] == ["wlr-randr", "--output", "HDMI-1", "--on"]
    assert "1920x1080" in calls[0]
    assert "0,0" in calls[0]


def test_make_output_exclusive_single_monitor_is_noop(monkeypatch):
    _tools(monkeypatch, {"wlr-randr"})
    assert make_output_exclusive({}, HDMI1, [HDMI1]) is None


def test_make_output_exclusive_requires_wlr_randr(monkeypatch):
    _tools(monkeypatch, set())
    assert make_output_exclusive({}, HDMI2, [HDMI1, HDMI2]) is None


def test_make_output_exclusive_none_when_all_fail(monkeypatch):
    _tools(monkeypatch, {"wlr-randr"})
    monkeypatch.setattr(
        "literaryclock.kiosk._run",
        lambda cmd, env=None, timeout=10.0, capture=False: (1, ""),
    )
    assert make_output_exclusive({}, HDMI2, [HDMI1, HDMI2]) is None


# --------------------------------------------------------------------------
# KioskProcess
# --------------------------------------------------------------------------
def test_kiosk_process_runs_cleanups_on_terminate():
    done: list[str] = []
    proc = KioskProcess(backend="wlr")
    proc.add_cleanup(lambda: done.append("first"))
    proc.add_cleanup(lambda: done.append("second"))
    proc.terminate()
    # 後入れ先出しで復元する
    assert done == ["second", "first"]


def test_kiosk_process_cleanup_errors_are_swallowed():
    def boom() -> None:
        raise RuntimeError("復元に失敗")

    proc = KioskProcess(backend="wlr")
    proc.add_cleanup(boom)
    proc.terminate()  # 例外が漏れないこと


def test_kiosk_process_poll_without_process():
    assert KioskProcess().poll() is None
