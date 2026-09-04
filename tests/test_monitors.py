"""monitors モジュール (マルチモニタ検出・選択) のテスト."""

from __future__ import annotations

import json

import pytest

from literaryclock.config import ConfigError, build_config
from literaryclock.display import _window_flags, select_monitor
from literaryclock.monitors import (
    Monitor,
    MonitorError,
    _mark_primary,
    detect_monitors,
    detect_sysfs,
    format_table,
    parse_swaymsg,
    parse_wlr_randr_json,
    parse_wlr_randr_text,
    parse_xrandr,
    resolve,
)

XRANDR_OUTPUT = """\
Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 16384 x 16384
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   1920x1080     60.00*+  50.00    59.94
HDMI-2 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+
DSI-1 disconnected (normal left inverted right x axis y axis)
"""

WLR_JSON = json.dumps(
    [
        {
            "name": "HDMI-A-1",
            "make": "Dell Inc.",
            "model": "U2415",
            "enabled": True,
            "position": {"x": 0, "y": 0},
            "modes": [
                {"width": 1920, "height": 1080, "current": False},
                {"width": 1200, "height": 1920, "current": True},
            ],
        },
        {
            "name": "HDMI-A-2",
            "make": "Unknown",
            "model": "Unknown",
            "enabled": True,
            "position": {"x": 1200, "y": 0},
            "modes": [{"width": 1920, "height": 1080, "current": True}],
        },
    ]
)

WLR_TEXT = """\
HDMI-A-1 "Dell Inc. U2415 ABC123"
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
  Position: 0,0
HDMI-A-2 "Sony TV"
  Enabled: yes
  Modes:
    3840x2160 px, 30.000000 Hz (preferred, current)
  Position: 1920,0
"""

SWAY_JSON = json.dumps(
    [
        {
            "name": "HDMI-A-1",
            "make": "Dell",
            "model": "U2415",
            "active": True,
            "primary": True,
            "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        },
        {
            "name": "HDMI-A-2",
            "make": "Sony",
            "model": "TV",
            "active": True,
            "primary": False,
            "rect": {"x": 1920, "y": 0, "width": 3840, "height": 2160},
        },
    ]
)


# --------------------------------------------------------------------------
# パーサ
# --------------------------------------------------------------------------
def test_parse_xrandr():
    mons = parse_xrandr(XRANDR_OUTPUT)
    assert [m.name for m in mons] == ["HDMI-1", "HDMI-2"]
    assert mons[0].primary is True
    assert mons[0].geometry == "1920x1080+0+0"
    assert mons[1].x == 1920
    assert mons[1].primary is False


def test_parse_xrandr_connected_but_inactive():
    text = "HDMI-2 connected (normal left inverted right x axis y axis)\n"
    mons = parse_xrandr(text)
    assert len(mons) == 1
    assert mons[0].active is False
    assert mons[0].width == 0


def test_parse_xrandr_empty():
    assert parse_xrandr("") == []
    assert parse_xrandr("HDMI-1 disconnected") == []


def test_parse_wlr_randr_json():
    mons = parse_wlr_randr_json(WLR_JSON)
    assert [m.name for m in mons] == ["HDMI-A-1", "HDMI-A-2"]
    # current: True のモードが使われる
    assert (mons[0].width, mons[0].height) == (1200, 1920)
    assert mons[0].description == "Dell Inc. U2415"
    # Unknown は説明から除外される
    assert mons[1].description == ""
    assert mons[1].x == 1200


def test_parse_wlr_randr_json_invalid():
    assert parse_wlr_randr_json("not json") == []
    assert parse_wlr_randr_json('{"a": 1}') == []


def test_parse_wlr_randr_text():
    mons = parse_wlr_randr_text(WLR_TEXT)
    assert [m.name for m in mons] == ["HDMI-A-1", "HDMI-A-2"]
    assert (mons[0].width, mons[0].height) == (1920, 1080)
    assert mons[1].x == 1920
    assert (mons[1].width, mons[1].height) == (3840, 2160)
    assert mons[0].active is True


def test_parse_swaymsg():
    mons = parse_swaymsg(SWAY_JSON)
    assert [m.name for m in mons] == ["HDMI-A-1", "HDMI-A-2"]
    assert mons[0].primary is True
    assert mons[1].geometry == "3840x2160+1920+0"


def test_detect_sysfs(tmp_path):
    for conn, status, modes in (
        ("card1-HDMI-A-1", "connected", "1920x1080\n1280x720\n"),
        ("card1-HDMI-A-2", "connected", "3840x2160\n"),
        ("card1-DSI-1", "disconnected", ""),
    ):
        d = tmp_path / conn
        d.mkdir()
        (d / "status").write_text(status, encoding="utf-8")
        if modes:
            (d / "modes").write_text(modes, encoding="utf-8")

    mons = detect_sysfs(tmp_path)
    assert [m.name for m in mons] == ["HDMI-A-1", "HDMI-A-2"]
    assert mons[0].primary is True
    # 座標不明のため、コネクタ順に左から並んでいると仮定する
    assert mons[0].x == 0
    assert mons[1].x == 1920


def test_detect_sysfs_missing_dir(tmp_path):
    assert detect_sysfs(tmp_path / "nope") == []


# --------------------------------------------------------------------------
# 選択 (resolve)
# --------------------------------------------------------------------------
@pytest.fixture
def two_monitors() -> list[Monitor]:
    return parse_xrandr(XRANDR_OUTPUT)


def test_resolve_empty_spec_returns_none(two_monitors):
    assert resolve("", two_monitors) is None
    assert resolve("   ", two_monitors) is None


def test_resolve_by_index(two_monitors):
    assert resolve("0", two_monitors).name == "HDMI-1"
    assert resolve("1", two_monitors).name == "HDMI-2"


def test_resolve_index_out_of_range(two_monitors):
    with pytest.raises(MonitorError) as exc:
        resolve("5", two_monitors)
    # エラーメッセージに候補一覧が含まれる
    assert "HDMI-1" in str(exc.value)
    assert "HDMI-2" in str(exc.value)


def test_resolve_by_connector_name(two_monitors):
    assert resolve("HDMI-2", two_monitors).name == "HDMI-2"


@pytest.mark.parametrize("spec", ["hdmi2", "HDMI2", "hdmi-2", "HDMI-2", " HDMI-2 "])
def test_resolve_name_is_lenient(two_monitors, spec):
    assert resolve(spec, two_monitors).name == "HDMI-2"


def test_resolve_wayland_x11_name_variants():
    """X11 の HDMI-2 表記で Wayland の HDMI-A-2 を指定できる (逆も可)."""
    wl = parse_wlr_randr_json(WLR_JSON)
    assert resolve("HDMI-2", wl).name == "HDMI-A-2"
    assert resolve("hdmi2", wl).name == "HDMI-A-2"
    assert resolve("HDMI-A-2", wl).name == "HDMI-A-2"


def test_resolve_keywords(two_monitors):
    assert resolve("primary", two_monitors).name == "HDMI-1"
    assert resolve("left", two_monitors).name == "HDMI-1"
    assert resolve("right", two_monitors).name == "HDMI-2"
    assert resolve("auto", two_monitors).name == "HDMI-1"


def test_resolve_top_bottom():
    mons = [
        Monitor(index=0, name="HDMI-1", width=1920, height=1080, x=0, y=1080),
        Monitor(index=1, name="HDMI-2", width=1920, height=1080, x=0, y=0),
    ]
    assert resolve("top", mons).name == "HDMI-2"
    assert resolve("bottom", mons).name == "HDMI-1"


def test_resolve_by_description():
    mons = parse_swaymsg(SWAY_JSON)
    assert resolve("Sony", mons).name == "HDMI-A-2"
    assert resolve("u2415", mons).name == "HDMI-A-1"


def test_resolve_ambiguous_partial_match():
    mons = [
        Monitor(index=0, name="HDMI-1", description="Dell U2415"),
        Monitor(index=1, name="HDMI-2", description="Dell U2720"),
    ]
    with pytest.raises(MonitorError) as exc:
        resolve("dell", mons)
    assert "複数" in str(exc.value)


def test_resolve_unknown_name(two_monitors):
    with pytest.raises(MonitorError):
        resolve("DP-9", two_monitors)


def test_resolve_no_monitors_detected():
    with pytest.raises(MonitorError) as exc:
        resolve("1", [])
    assert "検出できなかった" in str(exc.value)


def test_resolve_primary_falls_back_to_first():
    mons = [
        Monitor(index=0, name="HDMI-1", x=100),
        Monitor(index=1, name="HDMI-2", x=0),
    ]
    # primary フラグが無い場合でも必ず 1 台返る
    assert resolve("primary", mons) is not None


# --------------------------------------------------------------------------
# 一覧表示
# --------------------------------------------------------------------------
def test_format_table(two_monitors):
    text = format_table(two_monitors)
    assert "2 台" in text
    assert "HDMI-1" in text and "HDMI-2" in text
    assert "--monitor" in text


def test_format_table_empty():
    text = format_table([])
    assert "検出できませんでした" in text


def test_format_table_sysfs_warning(tmp_path):
    d = tmp_path / "card1-HDMI-A-1"
    d.mkdir()
    (d / "status").write_text("connected", encoding="utf-8")
    (d / "modes").write_text("1920x1080\n", encoding="utf-8")
    text = format_table(detect_sysfs(tmp_path))
    assert "/sys/class/drm" in text


# --------------------------------------------------------------------------
# Chromium フラグの組み立て
# --------------------------------------------------------------------------
def test_window_flags_none(monkeypatch):
    monkeypatch.delenv("LITCLOCK_WINDOW_POSITION", raising=False)
    monkeypatch.delenv("LITCLOCK_WINDOW_SIZE", raising=False)
    assert _window_flags(None) == []


def test_window_flags_from_monitor(monkeypatch):
    monkeypatch.delenv("LITCLOCK_WINDOW_POSITION", raising=False)
    monkeypatch.delenv("LITCLOCK_WINDOW_SIZE", raising=False)
    mon = parse_xrandr(XRANDR_OUTPUT)[1]
    flags = _window_flags(mon)
    assert "--window-position=1920,0" in flags
    assert "--window-size=1920,1080" in flags


def test_window_flags_explicit_wins_over_monitor(monkeypatch):
    monkeypatch.delenv("LITCLOCK_WINDOW_POSITION", raising=False)
    monkeypatch.delenv("LITCLOCK_WINDOW_SIZE", raising=False)
    mon = parse_xrandr(XRANDR_OUTPUT)[1]
    flags = _window_flags(mon, window_position="10,20", window_size="640,480")
    assert "--window-position=10,20" in flags
    assert "--window-size=640,480" in flags


def test_window_flags_env_still_supported(monkeypatch):
    """後方互換: 既存の LITCLOCK_WINDOW_* 環境変数も引き続き効く."""
    monkeypatch.setenv("LITCLOCK_WINDOW_POSITION", "1920,0")
    monkeypatch.setenv("LITCLOCK_WINDOW_SIZE", "1280,720")
    flags = _window_flags(None)
    assert "--window-position=1920,0" in flags
    assert "--window-size=1280,720" in flags


def test_window_flags_monitor_beats_env(monkeypatch):
    monkeypatch.setenv("LITCLOCK_WINDOW_POSITION", "0,0")
    monkeypatch.setenv("LITCLOCK_WINDOW_SIZE", "800,600")
    mon = parse_xrandr(XRANDR_OUTPUT)[1]
    flags = _window_flags(mon)
    assert "--window-position=1920,0" in flags
    assert "--window-size=1920,1080" in flags


def test_window_flags_monitor_without_size(monkeypatch):
    monkeypatch.delenv("LITCLOCK_WINDOW_POSITION", raising=False)
    monkeypatch.delenv("LITCLOCK_WINDOW_SIZE", raising=False)
    mon = Monitor(index=0, name="HDMI-1", x=1920, y=0)  # サイズ不明
    flags = _window_flags(mon)
    assert flags == ["--window-position=1920,0"]


# --------------------------------------------------------------------------
# select_monitor (フォールバック挙動)
# --------------------------------------------------------------------------
def test_select_monitor_empty_spec():
    assert select_monitor("") is None


def test_select_monitor_fallback(monkeypatch, caplog):
    monkeypatch.setattr("literaryclock.display.detect_monitors", lambda **kw: [])
    with caplog.at_level("WARNING"):
        assert select_monitor("1", fallback=True) is None
    assert "プライマリ" in caplog.text


def test_select_monitor_strict_raises(monkeypatch):
    monkeypatch.setattr("literaryclock.display.detect_monitors", lambda **kw: [])
    with pytest.raises(MonitorError):
        select_monitor("1", fallback=False)


def test_select_monitor_success(monkeypatch):
    mons = parse_xrandr(XRANDR_OUTPUT)
    monkeypatch.setattr("literaryclock.display.detect_monitors", lambda **kw: mons)
    chosen = select_monitor("HDMI-2")
    assert chosen is not None and chosen.name == "HDMI-2"


# --------------------------------------------------------------------------
# 設定 (config) との統合
# --------------------------------------------------------------------------
def test_config_monitor_defaults():
    cfg = build_config()
    assert cfg.monitor == ""
    assert cfg.monitor_fallback is True
    assert cfg.window_position == ""


def test_config_monitor_from_cli():
    cfg = build_config({"monitor": "HDMI-2", "monitor_fallback": False})
    assert cfg.monitor == "HDMI-2"
    assert cfg.monitor_fallback is False


def test_config_monitor_from_env(monkeypatch):
    monkeypatch.setenv("LITCLOCK_MONITOR", "1")
    monkeypatch.setenv("LITCLOCK_MONITOR_FALLBACK", "false")
    cfg = build_config()
    assert cfg.monitor == "1"
    assert cfg.monitor_fallback is False


def test_config_window_position_env(monkeypatch):
    monkeypatch.setenv("LITCLOCK_WINDOW_POSITION", "1920,0")
    cfg = build_config()
    assert cfg.window_position == "1920,0"


@pytest.mark.parametrize("value", ["1920,0", "-1920,0", "0,0"])
def test_config_window_position_valid(value):
    assert build_config({"window_position": value}).window_position == value


@pytest.mark.parametrize("value", ["1920", "a,b", "1920,0,0", "1920x1080"])
def test_config_window_position_invalid(value):
    with pytest.raises(ConfigError):
        build_config({"window_position": value})


def test_config_window_size_rejects_negative():
    with pytest.raises(ConfigError):
        build_config({"window_size": "-1,100"})
    with pytest.raises(ConfigError):
        build_config({"window_size": "0,100"})


def test_config_window_size_valid():
    assert build_config({"window_size": "1920,1080"}).window_size == "1920,1080"


# --------------------------------------------------------------------------
# DRM コネクタ名 (cage バックエンドが必要とする)
# --------------------------------------------------------------------------
def test_drm_connector_from_sysfs(tmp_path):
    d = tmp_path / "card1-HDMI-A-2"
    d.mkdir()
    (d / "status").write_text("connected", encoding="utf-8")
    (d / "modes").write_text("1920x1080\n", encoding="utf-8")
    mon = detect_sysfs(tmp_path)[0]
    assert mon.connector == "card1-HDMI-A-2"
    # card 接頭辞は落として DRM コネクタ名にする
    assert mon.drm_connector == "HDMI-A-2"


def test_drm_connector_normalizes_xrandr_name(two_monitors):
    """X11 の HDMI-2 を DRM 表記の HDMI-A-2 に変換する."""
    assert two_monitors[1].name == "HDMI-2"
    assert two_monitors[1].drm_connector == "HDMI-A-2"


def test_drm_connector_passthrough_for_unknown_kind():
    assert Monitor(index=0, name="DSI-1").drm_connector == "DSI-1"


def test_has_geometry():
    assert Monitor(index=0, name="HDMI-1", width=1920, height=1080).has_geometry
    assert not Monitor(index=0, name="HDMI-1").has_geometry


# --------------------------------------------------------------------------
# 環境変数を渡した検出 (SSH 経由対応)
# --------------------------------------------------------------------------
def test_detect_monitors_uses_supplied_env(monkeypatch):
    """SSH 経由でも env を渡せば xrandr が実行される."""
    seen = {}

    def fake_capture(cmd, timeout=5.0, env=None):
        seen["cmd"] = cmd
        seen["env"] = env
        return XRANDR_OUTPUT if cmd[0] == "xrandr" else None

    monkeypatch.setattr("literaryclock.monitors._run_capture", fake_capture)
    # プロセス自身の環境に DISPLAY は無い
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    mons = detect_monitors(env={"DISPLAY": ":0"})
    assert [m.name for m in mons] == ["HDMI-1", "HDMI-2"]
    assert seen["cmd"][0] == "xrandr"
    assert seen["env"]["DISPLAY"] == ":0"


def test_detect_monitors_wayland_env_prefers_wlr_randr(monkeypatch):
    called = []

    def fake_capture(cmd, timeout=5.0, env=None):
        called.append(cmd[0])
        return WLR_JSON if cmd[0] == "wlr-randr" else None

    monkeypatch.setattr("literaryclock.monitors._run_capture", fake_capture)
    mons = detect_monitors(env={"WAYLAND_DISPLAY": "wayland-0"})
    assert called[0] == "wlr-randr"
    assert [m.name for m in mons] == ["HDMI-A-1", "HDMI-A-2"]


def test_detect_monitors_falls_back_to_sysfs(monkeypatch, tmp_path):
    """GUI ツールが使えない場合でも sysfs から検出できる."""
    d = tmp_path / "card1-HDMI-A-1"
    d.mkdir()
    (d / "status").write_text("connected", encoding="utf-8")
    (d / "modes").write_text("1920x1080\n", encoding="utf-8")

    monkeypatch.setattr(
        "literaryclock.monitors._run_capture", lambda *a, **k: None
    )
    monkeypatch.setattr("literaryclock.monitors.DRM_DIR", tmp_path)
    monkeypatch.setattr(
        "literaryclock.monitors.detect_sysfs", lambda drm_dir=tmp_path: [
            Monitor(index=0, name="HDMI-A-1", width=1920, height=1080,
                    connector="card1-HDMI-A-1", source="sysfs")
        ]
    )
    mons = detect_monitors(env={})
    assert [m.name for m in mons] == ["HDMI-A-1"]


def test_resolve_accepts_env(monkeypatch):
    monkeypatch.setattr(
        "literaryclock.monitors.detect_monitors",
        lambda prefer="", env=None: parse_xrandr(XRANDR_OUTPUT),
    )
    assert resolve("1", env={"DISPLAY": ":0"}).name == "HDMI-2"


def test_mark_primary_preserves_connector():
    """primary 付与時に connector が失われないこと (dataclasses.replace)."""
    mons = _mark_primary([
        Monitor(index=0, name="HDMI-A-1", x=0, connector="card1-HDMI-A-1"),
        Monitor(index=1, name="HDMI-A-2", x=1920, connector="card1-HDMI-A-2"),
    ])
    assert mons[0].primary is True
    assert mons[0].connector == "card1-HDMI-A-1"
    assert mons[1].connector == "card1-HDMI-A-2"


# --------------------------------------------------------------------------
# display_backend 設定
# --------------------------------------------------------------------------
def test_config_display_backend_default():
    assert build_config().display_backend == "auto"


@pytest.mark.parametrize(
    "backend", ["auto", "x11", "sway", "wlr", "cage", "window"]
)
def test_config_display_backend_valid(backend):
    assert build_config({"display_backend": backend}).display_backend == backend


def test_config_display_backend_normalizes_case():
    assert build_config({"display_backend": "CAGE"}).display_backend == "cage"


def test_config_display_backend_invalid():
    with pytest.raises(ConfigError):
        build_config({"display_backend": "teleport"})


def test_config_display_backend_from_env(monkeypatch):
    monkeypatch.setenv("LITCLOCK_DISPLAY_BACKEND", "cage")
    assert build_config().display_backend == "cage"


def test_config_session_defaults():
    cfg = build_config()
    assert cfg.session == ""
    assert cfg.adopt_session is True
    assert cfg.exclusive_output is True


def test_config_session_from_env(monkeypatch):
    monkeypatch.setenv("LITCLOCK_SESSION", "wayland-0")
    monkeypatch.setenv("LITCLOCK_ADOPT_SESSION", "false")
    monkeypatch.setenv("LITCLOCK_EXCLUSIVE_OUTPUT", "no")
    cfg = build_config()
    assert cfg.session == "wayland-0"
    assert cfg.adopt_session is False
    assert cfg.exclusive_output is False
