"""接続されているディスプレイ (モニタ) の検出と選択.

Raspberry Pi 4 / 5 には HDMI 出力が 2 系統あるため、2 台のモニタを繋いだ状態でも
「どちらに時計を出すか」を簡単に指定できるようにする。

検出方法は環境に応じて best-effort で切り替える:

  1. X11        : ``xrandr --query``
  2. Wayland    : ``wlr-randr --json`` (labwc / wayfire) → テキスト出力 → ``swaymsg``
  3. フォールバック: ``/sys/class/drm/*/status`` (GUI ツールが無くても動く)

いずれも失敗した場合は空リストを返し、呼び出し側は「位置指定なし」で起動する。

コネクタ名は環境によって表記が違う (X11: ``HDMI-1`` / Wayland: ``HDMI-A-1``) ため、
:func:`resolve` では正規化した名前・番号・別名で緩く一致させる。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("literaryclock.monitors")

DRM_DIR = Path("/sys/class/drm")

# 位置キーワード (--monitor left など)
_POSITION_KEYWORDS = ("left", "right", "top", "bottom")
_PRIMARY_KEYWORDS = ("primary", "main", "auto")


class MonitorError(ValueError):
    """モニタ指定が解決できない場合に送出される."""


@dataclass(frozen=True)
class Monitor:
    """1 台のディスプレイ出力."""

    index: int
    name: str            # コネクタ名 (HDMI-1 / HDMI-A-1 / DSI-1 など)
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    primary: bool = False
    active: bool = True
    description: str = ""  # メーカー名・モデル名 (取得できた場合)
    source: str = ""       # 検出に使った方法 (xrandr / wlr-randr / sysfs ...)

    @property
    def geometry(self) -> str:
        """``1920x1080+0+0`` 形式の文字列."""
        return f"{self.width}x{self.height}+{self.x}+{self.y}"

    @property
    def position_arg(self) -> str:
        """Chromium の ``--window-position`` に渡す値."""
        return f"{self.x},{self.y}"

    @property
    def size_arg(self) -> str:
        """Chromium の ``--window-size`` に渡す値."""
        return f"{self.width},{self.height}"

    def label(self) -> str:
        """一覧表示用の 1 行."""
        mark = "*" if self.primary else " "
        state = "" if self.active else "  (未使用)"
        desc = f"  {self.description}" if self.description else ""
        return f"[{self.index}]{mark} {self.name:<12} {self.geometry:<18}{desc}{state}"

    def aliases(self) -> set[str]:
        """一致判定に使う別名の集合 (すべて正規化済み)."""
        names = {_normalize(self.name), str(self.index)}
        # HDMI-A-1 → hdmi1 / HDMI-1 → hdmi1 のように種別+番号へ畳む
        kind, number = _split_connector(self.name)
        if kind and number:
            names.add(f"{kind}{number}")
            names.add(f"{kind}-{number}")
        if self.description:
            names.add(_normalize(self.description))
        return names


# --------------------------------------------------------------------------
# 文字列ユーティリティ
# --------------------------------------------------------------------------
def _normalize(text: str) -> str:
    """比較用に小文字化して記号を除く."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _split_connector(name: str) -> tuple[str, str]:
    """コネクタ名を (種別, 番号) に分解する.

    ``HDMI-A-1`` → ``("hdmi", "1")`` / ``DP-2`` → ``("dp", "2")``
    Wayland の ``-A-`` (コネクタ種別 A) は番号ではないので取り除く。
    """
    match = re.match(r"^([A-Za-z]+)(?:-?[A-Za-z])?-?(\d+)$", name.strip())
    if not match:
        return "", ""
    return match.group(1).lower(), match.group(2)


def _run_capture(cmd: list[str], timeout: float = 5.0) -> str | None:
    """コマンドを実行して標準出力を返す (失敗時は None)."""
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s の実行に失敗: %s", cmd[0], exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


# --------------------------------------------------------------------------
# パーサ (テスト可能なよう純粋関数として分離)
# --------------------------------------------------------------------------
_XRANDR_RE = re.compile(
    r"^(?P<name>\S+)\s+connected\s*(?P<primary>primary\s+)?"
    r"(?:(?P<w>\d+)x(?P<h>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+))?"
)


def parse_xrandr(text: str) -> list[Monitor]:
    """``xrandr --query`` の出力を解析する."""
    monitors: list[Monitor] = []
    for line in text.splitlines():
        if " connected" not in line:
            continue
        m = _XRANDR_RE.match(line)
        if not m:
            continue
        active = bool(m.group("w"))
        monitors.append(
            Monitor(
                index=len(monitors),
                name=m.group("name"),
                width=int(m.group("w") or 0),
                height=int(m.group("h") or 0),
                x=int(m.group("x") or 0),
                y=int(m.group("y") or 0),
                primary=bool(m.group("primary")),
                active=active,
                source="xrandr",
            )
        )
    return monitors


def parse_wlr_randr_json(text: str) -> list[Monitor]:
    """``wlr-randr --json`` の出力を解析する."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    monitors: list[Monitor] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        width = height = 0
        for mode in item.get("modes") or []:
            if isinstance(mode, dict) and mode.get("current"):
                width = int(mode.get("width") or 0)
                height = int(mode.get("height") or 0)
                break
        pos = item.get("position") or {}
        desc = " ".join(
            str(item.get(key)).strip()
            for key in ("make", "model")
            if item.get(key) and str(item.get(key)).strip() not in ("Unknown", "unknown")
        ).strip()
        monitors.append(
            Monitor(
                index=len(monitors),
                name=str(item["name"]),
                width=width,
                height=height,
                x=int(pos.get("x") or 0) if isinstance(pos, dict) else 0,
                y=int(pos.get("y") or 0) if isinstance(pos, dict) else 0,
                primary=False,
                active=bool(item.get("enabled", True)),
                description=desc,
                source="wlr-randr",
            )
        )
    return monitors


def parse_wlr_randr_text(text: str) -> list[Monitor]:
    """``wlr-randr`` (JSON 非対応版) のテキスト出力を解析する."""
    monitors: list[Monitor] = []
    name = desc = ""
    width = height = x = y = 0
    enabled = True

    def flush() -> None:
        nonlocal name, desc, width, height, x, y, enabled
        if name:
            monitors.append(
                Monitor(
                    index=len(monitors),
                    name=name,
                    width=width,
                    height=height,
                    x=x,
                    y=y,
                    active=enabled,
                    description=desc,
                    source="wlr-randr",
                )
            )
        name = desc = ""
        width = height = x = y = 0
        enabled = True

    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            flush()
            head = raw.strip().split(None, 1)
            name = head[0]
            if len(head) > 1:
                desc = head[1].strip().strip('"')
            continue
        line = raw.strip()
        if line.startswith("Enabled:"):
            enabled = line.split(":", 1)[1].strip().lower() in ("yes", "true", "1")
        elif line.startswith("Position:"):
            coords = line.split(":", 1)[1].strip().replace(" ", "")
            if "," in coords:
                sx, _, sy = coords.partition(",")
                try:
                    x, y = int(sx), int(sy)
                except ValueError:
                    x = y = 0
        elif "current" in line and "px" in line:
            m = re.search(r"(\d+)x(\d+)\s*px", line)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
    flush()
    return monitors


def parse_swaymsg(text: str) -> list[Monitor]:
    """``swaymsg -t get_outputs -r`` の出力を解析する."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    monitors: list[Monitor] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        rect = item.get("rect") or {}
        desc = " ".join(
            str(item.get(key)).strip()
            for key in ("make", "model")
            if item.get(key) and str(item.get(key)).strip() not in ("Unknown", "unknown")
        ).strip()
        monitors.append(
            Monitor(
                index=len(monitors),
                name=str(item["name"]),
                width=int(rect.get("width") or 0),
                height=int(rect.get("height") or 0),
                x=int(rect.get("x") or 0),
                y=int(rect.get("y") or 0),
                primary=bool(item.get("primary")),
                active=bool(item.get("active", True)),
                description=desc,
                source="swaymsg",
            )
        )
    return monitors


def detect_sysfs(drm_dir: Path = DRM_DIR) -> list[Monitor]:
    """``/sys/class/drm`` から接続済み出力を列挙する (GUI ツール不要).

    レイアウト (X 座標) は分からないため、コネクタ順に左から並んでいると仮定する。
    Raspberry Pi では HDMI-A-1 = 基板の HDMI0 ポート, HDMI-A-2 = HDMI1 ポート。
    """
    if not drm_dir.is_dir():
        return []

    monitors: list[Monitor] = []
    offset = 0
    for card in sorted(drm_dir.iterdir(), key=lambda p: p.name):
        status_file = card / "status"
        if not status_file.is_file():
            continue
        try:
            status = status_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if status != "connected":
            continue

        # "card1-HDMI-A-1" → "HDMI-A-1"
        name = re.sub(r"^card\d+-", "", card.name)
        width = height = 0
        modes_file = card / "modes"
        if modes_file.is_file():
            try:
                first = modes_file.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                first = ""
            m = re.match(r"(\d+)x(\d+)", first.splitlines()[0] if first else "")
            if m:
                width, height = int(m.group(1)), int(m.group(2))

        monitors.append(
            Monitor(
                index=len(monitors),
                name=name,
                width=width,
                height=height,
                x=offset,
                y=0,
                primary=(len(monitors) == 0),
                active=True,
                source="sysfs",
            )
        )
        offset += width
    return monitors


# --------------------------------------------------------------------------
# 検出
# --------------------------------------------------------------------------
def detect_monitors(prefer: str = "") -> list[Monitor]:
    """接続されているディスプレイを検出する.

    prefer に ``xrandr`` / ``wlr-randr`` / ``swaymsg`` / ``sysfs`` を指定すると
    その方法を最初に試す (主にテスト・デバッグ用)。
    """
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = bool(os.environ.get("DISPLAY"))

    order: list[str] = []
    if prefer:
        order.append(prefer)
    if wayland:
        order += ["wlr-randr", "swaymsg", "xrandr"]
    elif x11:
        order += ["xrandr", "wlr-randr", "swaymsg"]
    else:
        order += ["xrandr", "wlr-randr", "swaymsg"]
    order.append("sysfs")

    seen: set[str] = set()
    for method in order:
        if method in seen:
            continue
        seen.add(method)

        monitors: list[Monitor] = []
        if method == "xrandr" and x11:
            out = _run_capture(["xrandr", "--query"])
            monitors = parse_xrandr(out) if out else []
        elif method == "wlr-randr" and wayland:
            out = _run_capture(["wlr-randr", "--json"])
            monitors = parse_wlr_randr_json(out) if out else []
            if not monitors:
                out = _run_capture(["wlr-randr"])
                monitors = parse_wlr_randr_text(out) if out else []
        elif method == "swaymsg" and wayland:
            out = _run_capture(["swaymsg", "-t", "get_outputs", "-r"])
            monitors = parse_swaymsg(out) if out else []
        elif method == "sysfs":
            monitors = detect_sysfs()

        if monitors:
            log.debug("%s で %d 台のディスプレイを検出", method, len(monitors))
            return _mark_primary(monitors)

    log.debug("ディスプレイを検出できませんでした")
    return []


def _mark_primary(monitors: list[Monitor]) -> list[Monitor]:
    """primary が 1 つも無い場合、左上に近いものを primary とみなす."""
    if not monitors or any(m.primary for m in monitors):
        return monitors
    active = [m for m in monitors if m.active] or monitors
    head = min(active, key=lambda m: (m.x, m.y))
    return [
        Monitor(**{**vars(m), "primary": (m is head)})  # type: ignore[arg-type]
        for m in monitors
    ]


# --------------------------------------------------------------------------
# 選択
# --------------------------------------------------------------------------
def resolve(spec: str, monitors: list[Monitor] | None = None) -> Monitor | None:
    """``--monitor`` の指定から 1 台を選ぶ.

    受け付ける書式:
      - ``""``            → None (指定なし)
      - ``"0"`` / ``"1"`` → 検出順のインデックス
      - ``"primary"``     → プライマリ (無ければ左上)
      - ``"left"`` / ``"right"`` / ``"top"`` / ``"bottom"`` → 位置で選択
      - ``"HDMI-2"`` / ``"hdmi2"`` / ``"HDMI-A-2"`` → コネクタ名 (表記ゆれ許容)
      - ``"Dell"``        → メーカー・モデル名の部分一致

    見つからない場合は :class:`MonitorError` を送出する。
    """
    spec = (spec or "").strip()
    if not spec:
        return None

    if monitors is None:
        monitors = detect_monitors()
    if not monitors:
        raise MonitorError(
            f"ディスプレイを検出できなかったため --monitor {spec!r} を解決できません。\n"
            "  GUI セッション上で実行しているか確認するか、\n"
            "  --window-position / --window-size で直接指定してください。"
        )

    usable = [m for m in monitors if m.active] or monitors
    key = _normalize(spec)

    # 1. インデックス指定 (0 始まり)
    if key.isdigit():
        idx = int(key)
        for m in monitors:
            if m.index == idx:
                return m
        raise MonitorError(_not_found(spec, monitors, f"インデックス {idx} は範囲外です"))

    # 2. キーワード
    if key in _PRIMARY_KEYWORDS:
        for m in usable:
            if m.primary:
                return m
        return usable[0]
    if key in _POSITION_KEYWORDS:
        if key == "left":
            return min(usable, key=lambda m: m.x)
        if key == "right":
            return max(usable, key=lambda m: m.x)
        if key == "top":
            return min(usable, key=lambda m: m.y)
        return max(usable, key=lambda m: m.y)

    # 3. コネクタ名 (完全一致 → 別名一致)
    for m in monitors:
        if key in m.aliases():
            return m

    # 4. 部分一致 (メーカー名・モデル名など)
    partial = [m for m in monitors if key and key in _normalize(f"{m.name} {m.description}")]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(m.name for m in partial)
        raise MonitorError(
            f"--monitor {spec!r} が複数のディスプレイに一致します: {names}\n"
            "  インデックスかコネクタ名で指定してください。"
        )

    raise MonitorError(_not_found(spec, monitors))


def _not_found(spec: str, monitors: list[Monitor], reason: str = "") -> str:
    lines = [f"--monitor {spec!r} に一致するディスプレイがありません。"]
    if reason:
        lines.append(f"  {reason}")
    lines.append("  検出済みのディスプレイ:")
    lines += [f"    {m.label()}" for m in monitors]
    lines.append("  例: --monitor 0 / --monitor 1 / --monitor HDMI-2 / --monitor right")
    return "\n".join(lines)


def format_table(monitors: list[Monitor]) -> str:
    """``literary-clock monitors`` 用の一覧文字列を作る."""
    if not monitors:
        return (
            "ディスプレイを検出できませんでした。\n"
            "  GUI セッション (X11 / Wayland) 上で実行しているか確認してください。\n"
            "  検出できない環境では --window-position / --window-size で直接指定できます。"
        )

    lines = [f"検出されたディスプレイ: {len(monitors)} 台  (* = プライマリ)", ""]
    lines += [f"  {m.label()}" for m in monitors]
    lines += [
        "",
        "使い方:",
        f"  literary-clock --monitor {monitors[-1].index}",
        f"  literary-clock --monitor {monitors[-1].name}",
        f"  LITCLOCK_MONITOR={monitors[-1].index} literary-clock",
    ]
    if any(m.source == "sysfs" for m in monitors):
        lines += [
            "",
            "注意: GUI ツール (xrandr / wlr-randr) が使えなかったため /sys/class/drm から",
            "      推定しました。配置座標は「コネクタ順に左から並んでいる」仮定です。",
        ]
    return "\n".join(lines)
