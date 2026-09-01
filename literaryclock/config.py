"""設定の読み込み・マージ・検証.

設定の優先順位 (後のものが優先):
  1. 組み込みデフォルト (DEFAULTS)
  2. 設定ファイル (config.json / config.toml)
  3. 環境変数 (LITCLOCK_*)
  4. コマンドライン引数
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 設定ファイルの探索順
CONFIG_SEARCH_PATHS = (
    Path("config.json"),
    Path.home() / ".config" / "literaryclock" / "config.json",
    Path("/etc/literaryclock/config.json"),
)

THEMES = ("ink", "washi", "night", "sepia", "mono")
WRITING_MODES = ("horizontal", "vertical", "auto")
TRANSITIONS = ("fade", "typewriter", "blur", "slide", "none")

DEFAULTS: dict[str, Any] = {
    # --- データ ---
    "dataset": "data/literary_clock.json",
    # --- サーバ ---
    "host": "127.0.0.1",
    "port": 8730,
    # --- 表示 ---
    "theme": "ink",
    "writing_mode": "horizontal",
    "transition": "fade",
    # 引用文の基準フォントサイズ (vmin 単位). 長文は自動で縮小される
    "font_scale": 1.0,
    # 時刻部分 (excerpt) を強調表示する
    "highlight_excerpt": True,
    # 作者・作品名を表示する
    "show_credit": True,
    # 画面下部に HH:MM のデジタル時刻を薄く表示する
    "show_digital_clock": True,
    # 秒針的な進行インジケータ (次のスロットまでの進捗バー)
    "show_progress": True,
    # 同一スロット内で複数候補がある場合にローテーションする間隔 (秒/0で無効)
    "rotate_seconds": 0,
    # --- 動作 ---
    # 起動時にブラウザを kiosk モードで開く
    "kiosk": True,
    # ブラウザ実行ファイル (空なら自動検出)
    "browser": "",
    # マウスカーソルを隠す (unclutter があれば使用)
    "hide_cursor": True,
    # 画面のスクリーンセーバ/DPMS を無効化する
    "disable_blanking": True,
    # 起動時刻を上書き (デバッグ用, "HH:MM" または空)
    "fake_time": "",
    # 時刻の進行を N 倍速にする (デモ用, 1.0 で実時間)
    "time_speed": 1.0,
}

_ENV_PREFIX = "LITCLOCK_"
_BOOL_KEYS = {
    "highlight_excerpt",
    "show_credit",
    "show_digital_clock",
    "show_progress",
    "kiosk",
    "hide_cursor",
    "disable_blanking",
}
_INT_KEYS = {"port", "rotate_seconds"}
_FLOAT_KEYS = {"font_scale", "time_speed"}


class ConfigError(ValueError):
    """設定値が不正な場合に送出される."""


@dataclass
class Config:
    """実行時設定."""

    values: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 委譲
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    # --- 表示用にフロントエンドへ渡す設定 ---
    def client_settings(self) -> dict[str, Any]:
        keys = (
            "theme",
            "writing_mode",
            "transition",
            "font_scale",
            "highlight_excerpt",
            "show_credit",
            "show_digital_clock",
            "show_progress",
            "rotate_seconds",
            "fake_time",
            "time_speed",
        )
        return {k: self.values[k] for k in keys}

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


def _coerce(key: str, raw: Any) -> Any:
    """文字列などの生値を設定キーに応じた型へ変換する."""
    if key in _BOOL_KEYS:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if key in _INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} には整数を指定してください: {raw!r}") from exc
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} には数値を指定してください: {raw!r}") from exc
    return raw


def _validate(values: dict[str, Any]) -> None:
    if values["theme"] not in THEMES:
        raise ConfigError(f"theme は {THEMES} のいずれかです: {values['theme']!r}")
    if values["writing_mode"] not in WRITING_MODES:
        raise ConfigError(
            f"writing_mode は {WRITING_MODES} のいずれかです: {values['writing_mode']!r}"
        )
    if values["transition"] not in TRANSITIONS:
        raise ConfigError(
            f"transition は {TRANSITIONS} のいずれかです: {values['transition']!r}"
        )
    if not (0 <= values["port"] <= 65535):
        raise ConfigError(f"port は 0..65535 の範囲です: {values['port']}")
    if not (0.4 <= values["font_scale"] <= 3.0):
        raise ConfigError(f"font_scale は 0.4..3.0 の範囲です: {values['font_scale']}")
    if values["time_speed"] <= 0:
        raise ConfigError("time_speed は正の数を指定してください")
    if values["rotate_seconds"] < 0:
        raise ConfigError("rotate_seconds は 0 以上を指定してください")
    fake = values.get("fake_time") or ""
    if fake:
        parts = fake.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ConfigError(f"fake_time は HH:MM 形式で指定してください: {fake!r}")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ConfigError(f"fake_time が範囲外です: {fake!r}")


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    """設定ファイルを読む. path が None の場合は既定の場所を探索する."""
    candidates = [path] if path else list(CONFIG_SEARCH_PATHS)
    for cand in candidates:
        if cand and cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"設定ファイルの JSON が不正です ({cand}): {exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError(f"設定ファイルはオブジェクト形式にしてください: {cand}")
            return {k: v for k, v in data.items() if k in DEFAULTS}
    return {}


def load_env() -> dict[str, Any]:
    """LITCLOCK_* 環境変数から設定を取り出す."""
    out: dict[str, Any] = {}
    for key in DEFAULTS:
        env_name = _ENV_PREFIX + key.upper()
        if env_name in os.environ:
            out[key] = _coerce(key, os.environ[env_name])
    return out


def build_config(
    cli_overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> Config:
    """デフォルト → ファイル → 環境変数 → CLI の順にマージした Config を返す."""
    values = dict(DEFAULTS)

    for source in (load_config_file(config_path), load_env()):
        for key, raw in source.items():
            if key in DEFAULTS:
                values[key] = _coerce(key, raw)

    for key, raw in (cli_overrides or {}).items():
        if raw is None or key not in DEFAULTS:
            continue
        values[key] = _coerce(key, raw)

    _validate(values)
    return Config(values)
