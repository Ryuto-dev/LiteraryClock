"""session モジュール (SSH 経由での GUI セッション探索) のテスト."""

from __future__ import annotations

import os

import pytest

from literaryclock.session import (
    GUI_ENV_KEYS,
    GuiSession,
    adopt,
    discover_sessions,
    ensure_gui_session,
    format_sessions,
    is_forwarded_display,
    is_remote_shell,
    parse_environ_blob,
    parse_loginctl_list,
    parse_loginctl_show,
    scan_wayland_sockets,
    scan_x11_displays,
    select_session,
    session_from_environ,
    sessions_from_proc,
    sessions_from_sockets,
)


# --------------------------------------------------------------------------
# リモートシェル / X11 転送の判定
# --------------------------------------------------------------------------
def test_is_remote_shell_detects_ssh():
    assert is_remote_shell({"SSH_CONNECTION": "10.0.0.2 5000 10.0.0.9 22"}) is True
    assert is_remote_shell({"SSH_TTY": "/dev/pts/0"}) is True
    assert is_remote_shell({}) is False


def test_is_remote_shell_uses_os_environ(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    assert is_remote_shell() is False
    monkeypatch.setenv("SSH_CLIENT", "10.0.0.2 5000 22")
    assert is_remote_shell() is True


@pytest.mark.parametrize(
    "display",
    ["localhost:10.0", "localhost:11.0", "127.0.0.1:10.0", "::1:10.0"],
)
def test_forwarded_display_over_tcp(display):
    """ssh -X の DISPLAY は「手元の PC の画面」なので使ってはいけない."""
    assert is_forwarded_display(display, {"SSH_CONNECTION": "x"}) is True


def test_forwarded_display_high_number_with_ssh():
    assert is_forwarded_display(":10.0", {"SSH_CONNECTION": "x"}) is True
    assert is_forwarded_display(":12", {"SSH_TTY": "/dev/pts/1"}) is True


def test_local_display_is_not_forwarded():
    assert is_forwarded_display(":0", {}) is False
    assert is_forwarded_display(":0.0", {}) is False
    assert is_forwarded_display(":1", {"SSH_CONNECTION": "x"}) is False
    assert is_forwarded_display("", {}) is False


def test_remote_host_display_is_treated_as_foreign():
    assert is_forwarded_display("otherpc:0", {}) is True


def test_unix_prefixed_display_is_local():
    assert is_forwarded_display("unix:0", {}) is False


# --------------------------------------------------------------------------
# パーサ
# --------------------------------------------------------------------------
def test_parse_environ_blob():
    blob = b"DISPLAY=:0\0WAYLAND_DISPLAY=wayland-0\0BROKEN\0\0USER=pi\0"
    env = parse_environ_blob(blob)
    assert env["DISPLAY"] == ":0"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["USER"] == "pi"
    assert "BROKEN" not in env


def test_parse_environ_blob_accepts_str():
    assert parse_environ_blob("A=1\0B=2")["B"] == "2"


def test_parse_environ_blob_handles_values_with_equals():
    env = parse_environ_blob("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


LOGINCTL_LIST = """\
   1 1000 pi   seat0 tty7
   3 1000 pi   -     pts/0
"""


def test_parse_loginctl_list():
    rows = parse_loginctl_list(LOGINCTL_LIST)
    assert [r["session"] for r in rows] == ["1", "3"]
    assert rows[0]["uid"] == "1000"
    assert rows[0]["user"] == "pi"
    assert rows[0]["seat"] == "seat0"


def test_parse_loginctl_list_skips_header_and_footer():
    text = "SESSION UID USER SEAT TTY\n 1 1000 pi seat0 tty7\n\n1 sessions listed.\n"
    rows = parse_loginctl_list(text)
    assert len(rows) == 1
    assert rows[0]["session"] == "1"


def test_parse_loginctl_show():
    text = "Type=wayland\nDisplay=\nName=pi\nUser=1000\nSeat=seat0\nRemote=no\n"
    data = parse_loginctl_show(text)
    assert data["Type"] == "wayland"
    assert data["Name"] == "pi"
    assert data["Display"] == ""


# --------------------------------------------------------------------------
# ソケット走査
# --------------------------------------------------------------------------
def test_scan_wayland_sockets(tmp_path):
    (tmp_path / "wayland-0").touch()
    (tmp_path / "wayland-1").touch()
    (tmp_path / "wayland-0.lock").touch()
    (tmp_path / "bus").touch()
    assert scan_wayland_sockets(tmp_path) == ["wayland-0", "wayland-1"]


def test_scan_wayland_sockets_missing_dir(tmp_path):
    assert scan_wayland_sockets(tmp_path / "nope") == []


def test_scan_x11_displays(tmp_path):
    (tmp_path / "X0").touch()
    (tmp_path / "X10").touch()
    (tmp_path / "junk").touch()
    assert scan_x11_displays(tmp_path) == [":0", ":10"]


def test_scan_x11_displays_missing_dir(tmp_path):
    assert scan_x11_displays(tmp_path / "nope") == []


# --------------------------------------------------------------------------
# 環境変数からのセッション構築
# --------------------------------------------------------------------------
def test_session_from_environ_wayland():
    sess = session_from_environ(
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "XDG_SESSION_TYPE": "wayland",
        }
    )
    assert sess is not None
    assert sess.usable is True
    assert sess.remote is False
    assert sess.env["WAYLAND_DISPLAY"] == "wayland-0"


def test_session_from_environ_none_without_gui():
    assert session_from_environ({"USER": "pi"}) is None


def test_session_from_environ_marks_forwarded_as_remote():
    sess = session_from_environ(
        {"DISPLAY": "localhost:10.0", "SSH_CONNECTION": "10.0.0.2 22"}
    )
    assert sess is not None
    assert sess.remote is True
    # SSH 転送の画面は「表示先」として使えない
    assert sess.usable is False


def test_session_from_environ_local_x11_is_usable():
    sess = session_from_environ({"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"})
    assert sess is not None and sess.usable is True


# --------------------------------------------------------------------------
# /proc からの採取
# --------------------------------------------------------------------------
def _make_proc(tmp_path, pid: int, comm: str, env: dict[str, str]):
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "comm").write_text(comm + "\n", encoding="utf-8")
    blob = "\0".join(f"{k}={v}" for k, v in env.items())
    (d / "environ").write_text(blob, encoding="utf-8")
    return d


def test_sessions_from_proc_finds_compositor(tmp_path):
    _make_proc(
        tmp_path, 700, "labwc",
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "USER": "pi",
        },
    )
    _make_proc(tmp_path, 42, "bash", {"USER": "pi"})
    sessions = sessions_from_proc(tmp_path)
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess.wayland_display == "wayland-0"
    assert sess.compositor == "labwc"
    assert sess.source == "proc"
    assert sess.usable is True


def test_sessions_from_proc_ignores_non_gui_processes(tmp_path):
    _make_proc(tmp_path, 10, "sshd", {"DISPLAY": ":0"})
    assert sessions_from_proc(tmp_path) == []


def test_sessions_from_proc_skips_processes_without_display(tmp_path):
    _make_proc(tmp_path, 700, "labwc", {"USER": "pi"})
    assert sessions_from_proc(tmp_path) == []


def test_sessions_from_proc_skips_forwarded_display(tmp_path):
    """SSH 転送の DISPLAY を持つプロセスは候補にしない."""
    _make_proc(
        tmp_path, 800, "openbox",
        {"DISPLAY": "localhost:10.0", "SSH_CONNECTION": "10.0.0.2 22"},
    )
    assert sessions_from_proc(tmp_path) == []


def test_sessions_from_proc_x11(tmp_path):
    _make_proc(
        tmp_path, 500, "Xorg",
        {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"},
    )
    sessions = sessions_from_proc(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].display == ":0"
    assert sessions[0].compositor == "xorg"


def test_sessions_from_proc_dedupes_same_display(tmp_path):
    for pid, comm in ((500, "Xorg"), (501, "openbox")):
        _make_proc(tmp_path, pid, comm, {"DISPLAY": ":0"})
    assert len(sessions_from_proc(tmp_path)) == 1


def test_sessions_from_proc_missing_dir(tmp_path):
    assert sessions_from_proc(tmp_path / "nope") == []


def test_sessions_from_proc_only_picks_gui_env_keys(tmp_path):
    _make_proc(
        tmp_path, 700, "labwc",
        {"WAYLAND_DISPLAY": "wayland-0", "SECRET_TOKEN": "do-not-copy"},
    )
    sess = sessions_from_proc(tmp_path)[0]
    assert "SECRET_TOKEN" not in sess.env
    assert set(sess.env) <= set(GUI_ENV_KEYS) | {"XDG_RUNTIME_DIR"}


# --------------------------------------------------------------------------
# ソケット由来のセッション
# --------------------------------------------------------------------------
def test_sessions_from_sockets_wayland(tmp_path, monkeypatch):
    (tmp_path / "wayland-0").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("literaryclock.session.X11_SOCKET_DIR", tmp_path / "nox11")
    sessions = sessions_from_sockets(uid=1000)
    assert [s.wayland_display for s in sessions] == ["wayland-0"]
    assert sessions[0].source == "socket"


def test_sessions_from_sockets_ignores_high_x_displays(tmp_path, monkeypatch):
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X10").touch()  # SSH 転送の可能性が高いので候補外
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("literaryclock.session.X11_SOCKET_DIR", x11)
    assert sessions_from_sockets(uid=1000) == []


# --------------------------------------------------------------------------
# 選択
# --------------------------------------------------------------------------
@pytest.fixture
def two_sessions() -> list[GuiSession]:
    return [
        GuiSession(
            env={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
            session_type="wayland",
            source="proc",
        ),
        GuiSession(env={"DISPLAY": ":0"}, session_type="x11", source="socket"),
    ]


def test_select_session_auto_picks_first(two_sessions):
    assert select_session("", two_sessions).wayland_display == "wayland-0"
    assert select_session("auto", two_sessions).wayland_display == "wayland-0"


def test_select_session_none_returns_none(two_sessions):
    assert select_session("none", two_sessions) is None


def test_select_session_by_socket_name(two_sessions):
    assert select_session("wayland-0", two_sessions).wayland_display == "wayland-0"
    assert select_session(":0", two_sessions).display == ":0"


def test_select_session_by_type(two_sessions):
    assert select_session("x11", two_sessions).display == ":0"
    assert select_session("wayland", two_sessions).wayland_display == "wayland-0"


def test_select_session_display_variant(two_sessions):
    assert select_session(":0.0", two_sessions).display == ":0"


def test_select_session_unknown_returns_none(two_sessions):
    assert select_session("wayland-9", two_sessions) is None


def test_select_session_skips_remote_sessions():
    sessions = [
        GuiSession(env={"DISPLAY": "localhost:10.0"}, remote=True, source="environ"),
    ]
    assert select_session("auto", sessions) is None


def test_session_score_prefers_local_and_complete():
    remote = GuiSession(env={"DISPLAY": "localhost:10.0"}, remote=True)
    local = GuiSession(
        env={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}
    )
    assert local.score() > remote.score()


# --------------------------------------------------------------------------
# 環境変数の引き継ぎ
# --------------------------------------------------------------------------
def test_adopt_applies_env():
    env: dict[str, str] = {}
    sess = GuiSession(
        env={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}
    )
    applied = adopt(sess, env)
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert "WAYLAND_DISPLAY" in applied


def test_adopt_none_is_noop():
    env = {"A": "1"}
    assert adopt(None, env) == {}
    assert env == {"A": "1"}


def test_adopt_drops_forwarded_display_for_wayland_session():
    """SSH 転送の DISPLAY を残すと手元の PC に出てしまうため削除する."""
    env = {
        "DISPLAY": "localhost:10.0",
        "XAUTHORITY": "/home/pi/.Xauthority",
        "SSH_CONNECTION": "10.0.0.2 22",
    }
    sess = GuiSession(env={"WAYLAND_DISPLAY": "wayland-0"}, session_type="wayland")
    adopt(sess, env)
    assert "DISPLAY" not in env
    assert "XAUTHORITY" not in env
    assert env["WAYLAND_DISPLAY"] == "wayland-0"


def test_adopt_keeps_local_display_for_wayland_session():
    """XWayland のローカル DISPLAY は消さない."""
    env = {"DISPLAY": ":0"}
    sess = GuiSession(env={"WAYLAND_DISPLAY": "wayland-0"}, session_type="wayland")
    adopt(sess, env)
    assert env["DISPLAY"] == ":0"


def test_adopt_skips_identical_values():
    env = {"DISPLAY": ":0"}
    applied = adopt(GuiSession(env={"DISPLAY": ":0"}), env)
    assert applied == {}


# --------------------------------------------------------------------------
# ensure_gui_session (統合)
# --------------------------------------------------------------------------
def test_ensure_gui_session_uses_existing_local_env(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    called = {"discover": False}

    def _fail() -> list:
        called["discover"] = True
        return []

    monkeypatch.setattr("literaryclock.session.discover_sessions", _fail)
    sess, applied = ensure_gui_session("")
    assert sess is not None and sess.wayland_display == "wayland-0"
    # ローカル GUI 上ならわざわざ探索しない
    assert called["discover"] is False
    assert applied == {}


def test_ensure_gui_session_adopts_when_ssh(monkeypatch):
    """SSH 経由 (GUI 環境変数なし) では本体のセッションを探して引き継ぐ."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.9 22")

    found = GuiSession(
        env={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
        session_type="wayland",
        compositor="labwc",
        source="proc",
    )
    monkeypatch.setattr("literaryclock.session.discover_sessions", lambda: [found])

    sess, applied = ensure_gui_session("")
    assert sess is found
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert applied["WAYLAND_DISPLAY"] == "wayland-0"


def test_ensure_gui_session_replaces_forwarded_display(monkeypatch):
    """ssh -X の DISPLAY よりも本体の Wayland セッションを優先する."""
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.9 22")

    found = GuiSession(
        env={"WAYLAND_DISPLAY": "wayland-0"}, session_type="wayland", source="proc"
    )
    monkeypatch.setattr("literaryclock.session.discover_sessions", lambda: [found])

    sess, _ = ensure_gui_session("")
    assert sess is found
    assert os.environ.get("WAYLAND_DISPLAY") == "wayland-0"
    assert "DISPLAY" not in os.environ


def test_ensure_gui_session_disabled(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        "literaryclock.session.discover_sessions",
        lambda: pytest.fail("探索してはいけない"),
    )
    sess, applied = ensure_gui_session("", enabled=False)
    assert sess is None and applied == {}


def test_ensure_gui_session_spec_none(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    sess, applied = ensure_gui_session("none")
    assert applied == {}
    assert sess is not None and sess.wayland_display == "wayland-1"


def test_ensure_gui_session_explicit_spec_triggers_discovery(monkeypatch):
    """明示指定があれば、既存の環境変数があっても探索して差し替える."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    target = GuiSession(env={"WAYLAND_DISPLAY": "wayland-1"}, session_type="wayland")
    monkeypatch.setattr(
        "literaryclock.session.discover_sessions",
        lambda: [
            GuiSession(env={"WAYLAND_DISPLAY": "wayland-0"}, session_type="wayland"),
            target,
        ],
    )
    sess, _ = ensure_gui_session("wayland-1")
    assert sess is target
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-1"


def test_ensure_gui_session_no_session_found(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 22")
    monkeypatch.setattr("literaryclock.session.discover_sessions", lambda: [])
    sess, applied = ensure_gui_session("")
    assert sess is None and applied == {}


def test_discover_sessions_returns_list(monkeypatch):
    """実環境依存だが、例外を出さず list を返すことを確認する."""
    assert isinstance(discover_sessions(), list)


# --------------------------------------------------------------------------
# 表示
# --------------------------------------------------------------------------
def test_format_sessions_empty():
    text = format_sessions([])
    assert "検出できませんでした" in text
    assert "cage" in text


def test_format_sessions_lists_candidates(two_sessions):
    text = format_sessions(two_sessions)
    assert "2 件" in text
    assert "wayland-0" in text
    assert ":0" in text


def test_format_sessions_marks_remote():
    sess = GuiSession(
        env={"DISPLAY": "localhost:10.0"}, remote=True, source="environ"
    )
    assert "SSH 転送" in format_sessions([sess])


def test_gui_session_label_includes_source():
    sess = GuiSession(
        env={"WAYLAND_DISPLAY": "wayland-0"},
        session_type="wayland",
        compositor="labwc",
        source="proc",
    )
    label = sess.label()
    assert "wayland-0" in label and "labwc" in label and "proc" in label
