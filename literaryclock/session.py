"""ローカル GUI セッションの探索と環境変数の引き継ぎ (SSH 経由対策).

SSH でログインしたシェルには、Raspberry Pi 本体のデスクトップを操作するための
環境変数が入っていない。そのため従来は

  - ``DISPLAY`` / ``WAYLAND_DISPLAY`` が無く kiosk 起動が拒否される
  - ``xrandr`` / ``wlr-randr`` が使えずディスプレイ検出が sysfs 推定に落ちる
  - X11 転送 (``ssh -X``) 時は ``DISPLAY=localhost:10.0`` になり、
    **手元の PC 側** にブラウザが出てしまう

という問題が起きていた。

このモジュールは「Pi 本体で動いている GUI セッション」を自力で見つけ出し、
その環境変数を現在のプロセスへ引き継ぐ (adopt する)。探索は次の順で
best-effort に行う。

  1. 現在の環境変数 (SSH の X11 転送は除外する)
  2. ``loginctl`` (systemd-logind) のセッション一覧
  3. ``/proc/<pid>/environ`` — コンポジタ / セッションプロセスの環境変数
  4. ソケット走査 — ``$XDG_RUNTIME_DIR/wayland-*`` と ``/tmp/.X11-unix/X*``

いずれも読み取り専用の操作なので、失敗しても副作用は無い。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Mapping

log = logging.getLogger("literaryclock.session")

PROC_DIR = Path("/proc")
X11_SOCKET_DIR = Path("/tmp/.X11-unix")

# SSH 経由かどうかの判定に使う環境変数
SSH_ENV_HINTS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "SSH_ORIGINAL_COMMAND")

# 引き継ぐ環境変数 (GUI セッションを掴むために必要なもの)
GUI_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "SWAYSOCK",
    "I3SOCK",
    "XDG_SESSION_TYPE",
    "XDG_SESSION_ID",
    "XDG_SEAT",
    "XDG_CURRENT_DESKTOP",
    "XDG_DATA_DIRS",
    "WLR_RUNTIME_DIR",
    "QT_QPA_PLATFORM",
    "GDK_BACKEND",
)

# 環境変数の採取元として信頼できるプロセス名
# (Raspberry Pi OS Bookworm は labwc / wayfire, 以前は Xorg + openbox)
COMPOSITOR_PROCESSES = (
    # Wayland コンポジタ
    "labwc",
    "wayfire",
    "sway",
    "weston",
    "cage",
    "kwin_wayland",
    "gnome-shell",
    "mutter",
    "hyprland",
    "river",
    # X サーバ / X セッション
    "xorg",
    "x",
    "xwayland",
    "openbox",
    "mutter-x11-frames",
    # デスクトップセッション
    "lxsession",
    "startlxde-pi",
    "xfce4-session",
    "mate-session",
    "cinnamon-session",
    "plasmashell",
    "lxpanel",
    "wf-panel-pi",
    "pcmanfm",
    "xdg-desktop-portal-wlr",
    "pipewire",
    "wireplumber",
)

# コンポジタとして扱うプロセス名 (どの WM が動いているかの判定に使う)
_COMPOSITOR_KINDS = {
    "labwc": "labwc",
    "wayfire": "wayfire",
    "sway": "sway",
    "weston": "weston",
    "cage": "cage",
    "kwin_wayland": "kwin",
    "gnome-shell": "gnome",
    "mutter": "gnome",
    "hyprland": "hyprland",
    "river": "river",
    "openbox": "openbox",
    "xorg": "xorg",
    "x": "xorg",
}


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class GuiSession:
    """発見したローカル GUI セッション 1 件."""

    env: Mapping[str, str] = field(default_factory=dict)
    session_type: str = ""      # "wayland" / "x11"
    user: str = ""
    uid: int = -1
    seat: str = ""
    session_id: str = ""
    compositor: str = ""        # labwc / wayfire / sway / xorg ...
    pid: int = 0
    source: str = ""            # environ / loginctl / proc / socket
    remote: bool = False        # SSH の X11 転送など「手元ではない」画面

    # ------------------------------------------------------------------
    @property
    def display(self) -> str:
        return str(self.env.get("DISPLAY", ""))

    @property
    def wayland_display(self) -> str:
        return str(self.env.get("WAYLAND_DISPLAY", ""))

    @property
    def runtime_dir(self) -> str:
        return str(self.env.get("XDG_RUNTIME_DIR", ""))

    @property
    def usable(self) -> bool:
        """kiosk 表示先として使えるか (ローカルの画面か)."""
        if self.remote:
            return False
        return bool(self.wayland_display or self.display)

    def score(self) -> tuple:
        """候補の優先順位 (大きいほど優先)."""
        return (
            0 if self.remote else 1,
            1 if self.wayland_display or self.display else 0,
            # 環境変数が揃っているものを優先 (proc から採ったものが最も完全)
            1 if self.env.get("XDG_RUNTIME_DIR") else 0,
            1 if self.env.get("DBUS_SESSION_BUS_ADDRESS") else 0,
            1 if self.seat in ("seat0", "") else 0,
            len(self.env),
        )

    def label(self) -> str:
        kind = self.session_type or ("wayland" if self.wayland_display else "x11")
        target = self.wayland_display or self.display or "?"
        bits = [f"{kind}:{target}"]
        if self.user:
            bits.append(f"user={self.user}")
        if self.compositor:
            bits.append(self.compositor)
        if self.seat:
            bits.append(self.seat)
        bits.append(f"via {self.source}")
        if self.remote:
            bits.append("※SSH 転送 (手元の画面ではありません)")
        return "  ".join(bits)

    def env_summary(self) -> str:
        keys = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY")
        return " ".join(f"{k}={self.env[k]}" for k in keys if self.env.get(k))


# --------------------------------------------------------------------------
# 判定ユーティリティ (純粋関数 / テスト対象)
# --------------------------------------------------------------------------
def is_remote_shell(environ: Mapping[str, str] | None = None) -> bool:
    """SSH などのリモートシェルから実行されているか."""
    env = os.environ if environ is None else environ
    return any(env.get(key) for key in SSH_ENV_HINTS)


def is_forwarded_display(display: str, environ: Mapping[str, str] | None = None) -> bool:
    """``DISPLAY`` が SSH の X11 転送 (手元の PC の画面) を指しているか.

    ``ssh -X`` すると ``DISPLAY=localhost:10.0`` のような値が入る。これを
    そのまま使うと Pi ではなく **接続元の PC** にブラウザが表示されてしまう。

    判定基準:
      - ホスト名部分が ``localhost`` / ``127.0.0.1`` などの TCP 指定
      - ディスプレイ番号が 10 以上 (sshd の X11DisplayOffset 既定値が 10)
    """
    display = (display or "").strip()
    if not display:
        return False

    host, _, rest = display.rpartition(":")
    if not rest:
        return False
    number = rest.split(".", 1)[0]

    if host and host not in ("", "unix"):
        # TCP 経由の X11。SSH 転送であれば localhost を向いている。
        if host.lower() in ("localhost", "127.0.0.1", "::1", "ip6-localhost"):
            return True
        # 別ホストの X サーバも「Pi 本体の画面」ではない
        return True

    try:
        num = int(number)
    except ValueError:
        return False
    # ローカルの GUI は通常 :0 (稀に :1)。10 以上は SSH 転送の慣習。
    if num >= 10 and is_remote_shell(environ):
        return True
    return False


def parse_environ_blob(blob: bytes | str) -> dict[str, str]:
    """``/proc/<pid>/environ`` の NUL 区切りデータを辞書にする."""
    if isinstance(blob, bytes):
        text = blob.decode("utf-8", errors="replace")
    else:
        text = blob
    out: dict[str, str] = {}
    for item in text.split("\0"):
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        if key:
            out[key] = value
    return out


def parse_loginctl_list(text: str) -> list[dict[str, str]]:
    """``loginctl list-sessions --no-legend`` の出力を解析する.

    列は環境によって差があるため、1 列目をセッション ID、数値をおおまかに
    UID、それ以外の語をユーザー名 / seat として拾う緩いパーサにしている。
    """
    sessions: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("SESSION") or "sessions listed" in line:
            continue
        parts = line.split()
        if not parts:
            continue
        entry: dict[str, str] = {"session": parts[0]}
        for token in parts[1:]:
            if token.isdigit() and "uid" not in entry:
                entry["uid"] = token
            elif token.startswith("seat"):
                entry["seat"] = token
            elif token.startswith("tty") or token.startswith("pts"):
                entry.setdefault("tty", token)
            elif "user" not in entry:
                entry["user"] = token
        sessions.append(entry)
    return sessions


def parse_loginctl_show(text: str) -> dict[str, str]:
    """``loginctl show-session -p ... <id>`` の ``KEY=VALUE`` 出力を解析する."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def scan_wayland_sockets(runtime_dir: Path | str) -> list[str]:
    """``$XDG_RUNTIME_DIR`` から wayland ソケット名を列挙する."""
    base = Path(runtime_dir)
    if not base.is_dir():
        return []
    names: list[str] = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for item in entries:
        if not re.fullmatch(r"wayland-\d+", item.name):
            continue
        names.append(item.name)
    return names


def scan_x11_displays(socket_dir: Path | str = X11_SOCKET_DIR) -> list[str]:
    """``/tmp/.X11-unix`` から利用可能な ``:N`` を列挙する."""
    base = Path(socket_dir)
    if not base.is_dir():
        return []
    out: list[str] = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for item in entries:
        m = re.fullmatch(r"X(\d+)", item.name)
        if m:
            out.append(f":{m.group(1)}")
    return out


def _find_xauthority(uid: int, home: str = "", runtime_dir: str = "") -> str:
    """X11 の認証ファイルを推測する (Pi OS は ~/.Xauthority が一般的)."""
    candidates: list[Path] = []
    if runtime_dir:
        # lightdm / labwc 系はランタイムディレクトリに置くことがある
        candidates += [
            Path(runtime_dir) / "Xauthority",
            Path(runtime_dir) / "gdm" / "Xauthority",
        ]
    if home:
        candidates.append(Path(home) / ".Xauthority")
    candidates += [
        Path(f"/run/user/{uid}/Xauthority") if uid >= 0 else Path("/nonexistent"),
        Path("/var/run/lightdm/root/:0"),
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return ""


# --------------------------------------------------------------------------
# 探索
# --------------------------------------------------------------------------
def _capture(cmd: list[str], timeout: float = 5.0) -> str:
    if not shutil.which(cmd[0]):
        return ""
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
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def session_from_environ(environ: Mapping[str, str] | None = None) -> GuiSession | None:
    """現在の環境変数から GUI セッションを組み立てる (使えない場合は None)."""
    env = dict(os.environ if environ is None else environ)
    wayland = env.get("WAYLAND_DISPLAY", "")
    display = env.get("DISPLAY", "")
    if not (wayland or display):
        return None

    remote = bool(display) and not wayland and is_forwarded_display(display, env)
    picked = {k: env[k] for k in GUI_ENV_KEYS if env.get(k)}
    return GuiSession(
        env=picked,
        session_type=env.get("XDG_SESSION_TYPE", "wayland" if wayland else "x11"),
        user=env.get("USER", ""),
        uid=os.getuid() if hasattr(os, "getuid") else -1,
        seat=env.get("XDG_SEAT", ""),
        session_id=env.get("XDG_SESSION_ID", ""),
        source="environ",
        remote=remote,
    )


def sessions_from_loginctl() -> list[GuiSession]:
    """``loginctl`` から GUI セッションを列挙する."""
    listing = _capture(["loginctl", "list-sessions", "--no-legend"])
    if not listing:
        return []

    out: list[GuiSession] = []
    for entry in parse_loginctl_list(listing):
        sid = entry.get("session", "")
        if not sid:
            continue
        detail = parse_loginctl_show(
            _capture(
                [
                    "loginctl",
                    "show-session",
                    sid,
                    "-p", "Type",
                    "-p", "Display",
                    "-p", "Name",
                    "-p", "User",
                    "-p", "Seat",
                    "-p", "Active",
                    "-p", "State",
                    "-p", "Remote",
                    "-p", "Leader",
                ]
            )
        )
        stype = (detail.get("Type") or "").lower()
        if stype not in ("wayland", "x11", "mir"):
            continue
        if (detail.get("Remote") or "").lower() == "yes":
            continue

        try:
            uid = int(detail.get("User") or entry.get("uid") or -1)
        except ValueError:
            uid = -1
        user = detail.get("Name") or entry.get("user", "")
        runtime = f"/run/user/{uid}" if uid >= 0 else ""

        env: dict[str, str] = {}
        if runtime and Path(runtime).is_dir():
            env["XDG_RUNTIME_DIR"] = runtime
        if stype == "x11":
            disp = detail.get("Display") or ""
            if disp:
                env["DISPLAY"] = disp
        else:
            # logind は Wayland ソケット名を持たないのでランタイム走査で補う
            socks = scan_wayland_sockets(runtime) if runtime else []
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0]
            # labwc 配下でも XWayland が動いていることが多い
            disp = detail.get("Display") or ""
            if disp:
                env["DISPLAY"] = disp
        if uid >= 0:
            env.setdefault(
                "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"
            )
        env["XDG_SESSION_TYPE"] = stype
        if detail.get("Seat"):
            env["XDG_SEAT"] = detail["Seat"]
        if sid:
            env["XDG_SESSION_ID"] = sid
        if stype == "x11" and env.get("DISPLAY"):
            xauth = _find_xauthority(
                uid, home=str(Path("/home") / user) if user else "", runtime_dir=runtime
            )
            if xauth:
                env["XAUTHORITY"] = xauth

        try:
            pid = int(detail.get("Leader") or 0)
        except ValueError:
            pid = 0

        out.append(
            GuiSession(
                env=env,
                session_type=stype,
                user=user,
                uid=uid,
                seat=detail.get("Seat", ""),
                session_id=sid,
                pid=pid,
                source="loginctl",
            )
        )
    return out


def _iter_proc_candidates(proc_dir: Path) -> Iterable[tuple[int, str]]:
    """``/proc`` から (pid, プロセス名) を列挙する."""
    try:
        entries = sorted(
            (p for p in proc_dir.iterdir() if p.name.isdigit()),
            key=lambda p: int(p.name),
        )
    except OSError:
        return []
    for item in entries:
        try:
            name = (item / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if name:
            yield int(item.name), name


def sessions_from_proc(proc_dir: Path = PROC_DIR, uid: int | None = None) -> list[GuiSession]:
    """コンポジタ / セッションプロセスの ``environ`` から環境変数を採取する.

    自分と同じ UID のプロセスは ``/proc/<pid>/environ`` を読めるため、
    SSH ログインでも「デスクトップ側の環境変数」をそのまま入手できる。
    root であれば他ユーザーのプロセスも読める。
    """
    if not proc_dir.is_dir():
        return []

    wanted = {name.lower() for name in COMPOSITOR_PROCESSES}
    found: list[GuiSession] = []
    seen: set[tuple[str, str]] = set()

    for pid, comm in _iter_proc_candidates(proc_dir):
        if comm.lower() not in wanted:
            continue
        environ_path = proc_dir / str(pid) / "environ"
        try:
            blob = environ_path.read_bytes()
        except OSError:
            # 権限が無い (他ユーザーのプロセス) 場合はスキップ
            continue
        env = parse_environ_blob(blob)
        if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            continue

        display = env.get("DISPLAY", "")
        wayland = env.get("WAYLAND_DISPLAY", "")
        if not wayland and is_forwarded_display(display, env):
            continue

        key = (wayland, display)
        if key in seen:
            continue
        seen.add(key)

        try:
            proc_uid = (proc_dir / str(pid)).stat().st_uid
        except OSError:
            proc_uid = -1
        if uid is not None and proc_uid != uid:
            continue

        picked = {k: env[k] for k in GUI_ENV_KEYS if env.get(k)}
        if not picked.get("XDG_RUNTIME_DIR") and proc_uid >= 0:
            runtime = f"/run/user/{proc_uid}"
            if Path(runtime).is_dir():
                picked["XDG_RUNTIME_DIR"] = runtime

        found.append(
            GuiSession(
                env=picked,
                session_type=env.get(
                    "XDG_SESSION_TYPE", "wayland" if wayland else "x11"
                ),
                user=env.get("USER", ""),
                uid=proc_uid,
                seat=env.get("XDG_SEAT", ""),
                session_id=env.get("XDG_SESSION_ID", ""),
                compositor=_COMPOSITOR_KINDS.get(comm.lower(), comm),
                pid=pid,
                source="proc",
            )
        )
    return found


def sessions_from_sockets(uid: int | None = None) -> list[GuiSession]:
    """ソケットの存在だけを頼りにセッションを推測する (最後の手段)."""
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else -1
    out: list[GuiSession] = []

    runtime = os.environ.get("XDG_RUNTIME_DIR") or (
        f"/run/user/{uid}" if uid >= 0 else ""
    )
    if runtime:
        for sock in scan_wayland_sockets(runtime):
            env = {
                "WAYLAND_DISPLAY": sock,
                "XDG_RUNTIME_DIR": runtime,
                "XDG_SESSION_TYPE": "wayland",
            }
            if uid >= 0 and Path(f"/run/user/{uid}/bus").exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
            out.append(
                GuiSession(
                    env=env,
                    session_type="wayland",
                    uid=uid,
                    source="socket",
                )
            )

    for disp in scan_x11_displays():
        # :10 以上は SSH 転送の可能性が高いので候補にしない
        try:
            if int(disp.lstrip(":").split(".")[0]) >= 10:
                continue
        except ValueError:
            continue
        env = {"DISPLAY": disp, "XDG_SESSION_TYPE": "x11"}
        if runtime:
            env["XDG_RUNTIME_DIR"] = runtime
        xauth = _find_xauthority(uid, home=str(Path.home()), runtime_dir=runtime)
        if xauth:
            env["XAUTHORITY"] = xauth
        out.append(
            GuiSession(env=env, session_type="x11", uid=uid, source="socket")
        )
    return out


def _merge(base: GuiSession, extra: GuiSession) -> GuiSession:
    """同じ画面を指す候補を統合する (足りない環境変数を補完)."""
    env = dict(extra.env)
    env.update({k: v for k, v in base.env.items() if v})
    for key, value in extra.env.items():
        env.setdefault(key, value)
    return replace(
        base,
        env=env,
        compositor=base.compositor or extra.compositor,
        user=base.user or extra.user,
        uid=base.uid if base.uid >= 0 else extra.uid,
        seat=base.seat or extra.seat,
        session_id=base.session_id or extra.session_id,
        pid=base.pid or extra.pid,
        source=base.source
        if base.source == extra.source
        else f"{base.source}+{extra.source}",
    )


def _dedupe(sessions: list[GuiSession]) -> list[GuiSession]:
    """同じ画面を指す候補をまとめ、優先度の高い順に並べる."""
    merged: dict[tuple[str, str], GuiSession] = {}
    for sess in sessions:
        key = (sess.wayland_display, sess.display)
        if key in merged:
            merged[key] = _merge(merged[key], sess)
            continue
        # WAYLAND_DISPLAY が同じで DISPLAY 有無だけ違う場合も統合する
        hit = None
        for existing in list(merged):
            if sess.wayland_display and existing[0] == sess.wayland_display:
                hit = existing
                break
        if hit is not None:
            merged[hit] = _merge(merged[hit], sess)
        else:
            merged[key] = sess
    return sorted(merged.values(), key=lambda s: s.score(), reverse=True)


def discover_sessions(include_current: bool = True) -> list[GuiSession]:
    """ローカル GUI セッションの候補を優先度順に返す."""
    found: list[GuiSession] = []
    if include_current:
        current = session_from_environ()
        if current is not None:
            found.append(current)
    found += sessions_from_proc()
    found += sessions_from_loginctl()
    found += sessions_from_sockets()
    return _dedupe(found)


def select_session(spec: str = "", sessions: list[GuiSession] | None = None) -> GuiSession | None:
    """``--session`` の指定から 1 件選ぶ.

    受け付ける書式:
      - ``""`` / ``"auto"`` → 最も確度の高いローカル GUI セッション
      - ``"none"``          → 引き継がない (現在の環境変数のまま)
      - ``"wayland-0"``     → ``WAYLAND_DISPLAY`` 一致
      - ``":0"``            → ``DISPLAY`` 一致
      - ``"wayland"`` / ``"x11"`` → セッション種別
    """
    spec = (spec or "").strip()
    if spec.lower() == "none":
        return None
    if sessions is None:
        sessions = discover_sessions()
    usable = [s for s in sessions if s.usable]
    if not usable:
        return None
    if not spec or spec.lower() == "auto":
        return usable[0]

    key = spec.lower()
    if key in ("wayland", "x11"):
        for sess in usable:
            if sess.session_type.lower() == key or (
                key == "wayland" and sess.wayland_display
            ) or (key == "x11" and sess.display and not sess.wayland_display):
                return sess
        return None
    for sess in usable:
        if spec in (sess.wayland_display, sess.display):
            return sess
    # ":0.0" のような表記ゆれを許容
    for sess in usable:
        if sess.display and sess.display.split(".")[0] == spec.split(".")[0]:
            return sess
    return None


def adopt(session: GuiSession | None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """セッションの環境変数を現在のプロセスへ引き継ぐ.

    返り値は実際に適用した環境変数。``None`` を渡した場合は何もしない。
    """
    if session is None:
        return {}
    env = os.environ if environ is None else environ
    applied: dict[str, str] = {}
    for key in GUI_ENV_KEYS:
        value = session.env.get(key)
        if not value:
            continue
        if env.get(key) == value:
            continue
        env[key] = value
        applied[key] = value

    # SSH の X11 転送が残っていると Chromium が手元の PC に出てしまう。
    # Wayland セッションを掴めたなら、転送用の DISPLAY は捨てる。
    if session.wayland_display:
        current_display = env.get("DISPLAY", "")
        if current_display and not session.env.get("DISPLAY"):
            if is_forwarded_display(current_display, env):
                env.pop("DISPLAY", None)
                env.pop("XAUTHORITY", None)
                applied["DISPLAY"] = "(削除: SSH 転送)"
    return applied


def ensure_gui_session(
    spec: str = "",
    enabled: bool = True,
) -> tuple[GuiSession | None, dict[str, str]]:
    """必要に応じて GUI セッションを探し、環境変数を引き継ぐ.

    - ``enabled=False`` → 何もしない (従来通り現在の環境変数を使う)
    - すでにローカルの GUI 環境変数が揃っていれば探索を省略する
    - SSH の X11 転送しか無い場合は、本体側のセッションを探して差し替える
    """
    if not enabled and not spec:
        return session_from_environ(), {}
    if spec.strip().lower() == "none":
        return session_from_environ(), {}

    current = session_from_environ()
    explicit = bool(spec.strip()) and spec.strip().lower() != "auto"
    if current is not None and current.usable and not explicit:
        # ローカルの GUI セッション上で動いている (デスクトップの端末など)
        log.debug("既存の GUI 環境変数を使用します: %s", current.env_summary())
        return current, {}

    sessions = discover_sessions()
    chosen = select_session(spec, sessions)
    if chosen is None:
        if current is not None and current.remote:
            log.warning(
                "DISPLAY=%s は SSH の X11 転送のようです。"
                "Raspberry Pi 本体の画面を見つけられませんでした。",
                current.display,
            )
        return current, {}

    applied = adopt(chosen)
    if applied:
        log.info("GUI セッションを引き継ぎました: %s", chosen.label())
        log.debug("引き継いだ環境変数: %s", " ".join(f"{k}={v}" for k, v in applied.items()))
    return chosen, applied


def format_sessions(sessions: list[GuiSession] | None = None) -> str:
    """``literary-clock doctor`` 用のセッション一覧文字列."""
    if sessions is None:
        sessions = discover_sessions()
    if not sessions:
        return (
            "GUI セッションを検出できませんでした。\n"
            "  デスクトップに自動ログインしているか確認してください。\n"
            "  GUI が無い場合は --display-backend cage で直接 DRM に描画できます。"
        )
    lines = [f"検出した GUI セッション: {len(sessions)} 件  (上が優先)", ""]
    for i, sess in enumerate(sessions):
        mark = "→" if i == 0 and sess.usable else " "
        lines.append(f"  {mark} [{i}] {sess.label()}")
        if sess.env_summary():
            lines.append(f"        {sess.env_summary()}")
    return "\n".join(lines)
