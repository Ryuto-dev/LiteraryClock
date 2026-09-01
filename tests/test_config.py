"""config モジュールのテスト."""

from __future__ import annotations

import json

import pytest

from literaryclock.config import DEFAULTS, ConfigError, build_config, load_env


def test_defaults_applied():
    cfg = build_config()
    assert cfg.theme == DEFAULTS["theme"]
    assert cfg.port == DEFAULTS["port"]
    assert cfg.kiosk is True


def test_cli_overrides_win():
    cfg = build_config({"theme": "washi", "port": 9000})
    assert cfg.theme == "washi"
    assert cfg.port == 9000


def test_none_overrides_are_ignored():
    """argparse で未指定 (None) の項目はデフォルトを壊さない."""
    cfg = build_config({"theme": None, "port": None})
    assert cfg.theme == DEFAULTS["theme"]
    assert cfg.port == DEFAULTS["port"]


def test_unknown_keys_ignored():
    cfg = build_config({"nonexistent_key": "x"})
    assert "nonexistent_key" not in cfg.as_dict()


def test_config_file_read(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "night", "port": 8888}), encoding="utf-8")
    cfg = build_config(config_path=path)
    assert cfg.theme == "night"
    assert cfg.port == 8888


def test_cli_beats_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "night"}), encoding="utf-8")
    cfg = build_config({"theme": "mono"}, config_path=path)
    assert cfg.theme == "mono"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LITCLOCK_THEME", "sepia")
    monkeypatch.setenv("LITCLOCK_PORT", "9100")
    monkeypatch.setenv("LITCLOCK_KIOSK", "false")
    env = load_env()
    assert env["theme"] == "sepia"
    assert env["port"] == 9100
    assert env["kiosk"] is False


def test_env_beaten_by_cli(monkeypatch):
    monkeypatch.setenv("LITCLOCK_THEME", "sepia")
    cfg = build_config({"theme": "ink"})
    assert cfg.theme == "ink"


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("yes", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("", False)],
)
def test_bool_coercion(monkeypatch, raw, expected):
    monkeypatch.setenv("LITCLOCK_SHOW_CREDIT", raw)
    assert load_env()["show_credit"] is expected


# --------------------------------------------------------------------------
# 検証
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"theme": "rainbow"},
        {"writing_mode": "diagonal"},
        {"transition": "explode"},
        {"port": -1},
        {"port": 70000},
        {"font_scale": 0.1},
        {"font_scale": 9.0},
        {"time_speed": 0},
        {"time_speed": -1},
        {"rotate_seconds": -5},
        {"fake_time": "25:00"},
        {"fake_time": "16:70"},
        {"fake_time": "abc"},
        {"fake_time": "1640"},
    ],
)
def test_invalid_values_rejected(overrides):
    with pytest.raises(ConfigError):
        build_config(overrides)


@pytest.mark.parametrize("theme", ["ink", "washi", "night", "sepia", "mono"])
def test_all_themes_valid(theme):
    assert build_config({"theme": theme}).theme == theme


@pytest.mark.parametrize("mode", ["horizontal", "vertical", "auto"])
def test_all_writing_modes_valid(mode):
    assert build_config({"writing_mode": mode}).writing_mode == mode


@pytest.mark.parametrize(
    "transition", ["fade", "typewriter", "blur", "slide", "none"]
)
def test_all_transitions_valid(transition):
    assert build_config({"transition": transition}).transition == transition


def test_fake_time_accepted():
    assert build_config({"fake_time": "16:40"}).fake_time == "16:40"
    assert build_config({"fake_time": ""}).fake_time == ""


def test_bad_int_raises():
    with pytest.raises(ConfigError):
        build_config({"port": "not-a-number"})


def test_invalid_config_file_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        build_config(config_path=path)


def test_config_file_must_be_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        build_config(config_path=path)


# --------------------------------------------------------------------------
# クライアント設定
# --------------------------------------------------------------------------
def test_client_settings_excludes_server_only_keys():
    cs = build_config().client_settings()
    for key in ("theme", "writing_mode", "transition", "font_scale"):
        assert key in cs
    # サーバ専用の項目はフロントへ渡さない
    for key in ("host", "port", "dataset", "kiosk", "browser"):
        assert key not in cs
