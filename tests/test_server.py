"""server モジュールのテスト (実際に HTTP サーバを立てて検証する)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from literaryclock.config import build_config
from literaryclock.dataset import Dataset, slot_label
from literaryclock.server import ClockServer, available_fonts

SAMPLE = {
    "time": "16:40",
    "excerpt": "午後四時四十分",
    "before": "で、ちょっと行き渋ったが、",
    "after": "発の急行で、東京駅を立ったのだった。",
    "author": "大倉燁子",
    "title": "深夜の客",
}


@pytest.fixture
def server():
    """全 144 スロットを埋めたデータセットでサーバを起動する."""
    payload = [
        dict(SAMPLE, time=slot_label(i), excerpt=f"時刻{i}", quote=f"前{i}時刻{i}後")
        for i in range(144)
    ]
    dataset = Dataset.from_payload(payload)
    # ポート 0 で OS に空きポートを割り当てさせ、テストの並行実行でも衝突しない
    config = build_config({"port": 0, "host": "127.0.0.1", "kiosk": False})
    srv = ClockServer(config, dataset)
    srv.start()
    assert srv.wait_ready(timeout=5)
    yield srv
    srv.shutdown()


def get(server: ClockServer, path: str):
    with urllib.request.urlopen(server.url.rstrip("/") + path, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def get_raw(server: ClockServer, path: str):
    with urllib.request.urlopen(server.url.rstrip("/") + path, timeout=5) as res:
        # res.headers (http.client.HTTPMessage) は大文字小文字を無視して参照できる。
        # dict() 化すると実際に送られたケース (例: "Content-type") に固定されてしまい、
        # http.server が標準で送るケースと自作 API のケースが食い違って落ちるため、
        # そのまま email.message.Message として返す。
        return res.status, res.read(), res.headers


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def test_health(server):
    status, body = get(server, "/api/health")
    assert status == 200
    assert body["ok"] is True
    assert "version" in body


def test_bootstrap_shape(server):
    status, body = get(server, "/api/bootstrap")
    assert status == 200
    assert "settings" in body
    assert "dataset" in body
    assert "entry" in body
    assert body["dataset"]["entries"] == 144
    assert body["dataset"]["slots_filled"] == 144
    assert body["dataset"]["slots_total"] == 144
    assert body["dataset"]["missing"] == []
    # フロントが描画に必要とする項目が揃っていること
    for key in ("quote", "excerpt", "author", "title", "slot", "length"):
        assert key in body["entry"]


def test_now_returns_entry(server):
    status, body = get(server, "/api/now")
    assert status == 200
    assert 0 <= body["slot"] <= 143
    assert body["quote"]


def test_at_specific_time(server):
    status, body = get(server, "/api/at?time=16:40")
    assert status == 200
    assert body["time"] == "16:40"
    assert body["slot"] == 100
    assert body["exact"] is True


def test_at_rounds_down_to_slot(server):
    _, body = get(server, "/api/at?time=16:45")
    assert body["time"] == "16:40"


def test_at_requires_time_param(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/at")
    assert exc.value.code == 400


def test_at_rejects_invalid_time(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/at?time=99:99")
    assert exc.value.code == 400


def test_unknown_api_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/nope")
    assert exc.value.code == 404


def test_fonts_endpoint(server):
    status, body = get(server, "/api/fonts")
    assert status == 200
    assert isinstance(body["available"], list)


def test_json_is_utf8_and_not_cached(server):
    _, _, headers = get_raw(server, "/api/health")
    assert "utf-8" in headers["Content-Type"].lower()
    assert headers["Cache-Control"] == "no-store"


def test_japanese_is_not_ascii_escaped(server):
    """日本語がそのまま (エスケープされず) 返ること."""
    _, raw, _ = get_raw(server, "/api/at?time=16:40")
    assert "深夜の客".encode("utf-8") in raw


# --------------------------------------------------------------------------
# ローテーション
# --------------------------------------------------------------------------
def test_rotation_cycles_candidates():
    payload = [
        dict(SAMPLE, time="16:40", quote="一つ目午後四時四十分", excerpt="午後四時四十分"),
        dict(SAMPLE, time="16:40", quote="二つ目午後四時四十分", excerpt="午後四時四十分"),
    ]
    dataset = Dataset.from_payload(payload)
    config = build_config({"port": 0, "kiosk": False})
    srv = ClockServer(config, dataset)
    srv.start()
    try:
        assert srv.wait_ready(timeout=5)
        _, first = get(srv, "/api/at?time=16:40&rotation=0")
        _, second = get(srv, "/api/at?time=16:40&rotation=1")
        _, wrapped = get(srv, "/api/at?time=16:40&rotation=2")
        assert first["candidates"] == 2
        assert first["quote"] != second["quote"]
        assert wrapped["quote"] == first["quote"]
    finally:
        srv.shutdown()


def test_missing_slots_reported():
    """欠損スロットが bootstrap で報告され、代替表示されること."""
    dataset = Dataset.from_payload([dict(SAMPLE, time="16:40")])
    config = build_config({"port": 0, "kiosk": False, "fake_time": "18:00"})
    srv = ClockServer(config, dataset)
    srv.start()
    try:
        assert srv.wait_ready(timeout=5)
        _, body = get(srv, "/api/bootstrap")
        assert len(body["dataset"]["missing"]) == 143
        # 18:00 のエントリは無いので 16:40 で代替される
        assert body["entry"]["time"] == "16:40"
        assert body["entry"]["exact"] is False
    finally:
        srv.shutdown()


def test_fake_time_pins_the_clock():
    dataset = Dataset.from_payload(
        [dict(SAMPLE, time=slot_label(i), excerpt=f"時刻{i}", quote=f"前{i}時刻{i}後")
         for i in range(144)]
    )
    config = build_config({"port": 0, "kiosk": False, "fake_time": "03:30"})
    srv = ClockServer(config, dataset)
    srv.start()
    try:
        assert srv.wait_ready(timeout=5)
        _, body = get(srv, "/api/now")
        assert body["time"] == "03:30"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------
# 静的ファイル配信
# --------------------------------------------------------------------------
def test_serves_index_html(server):
    status, raw, headers = get_raw(server, "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"<html" in raw.lower()


@pytest.mark.parametrize(
    "path,mime",
    [
        ("/css/app.css", "text/css"),
        ("/css/fonts.css", "text/css"),
        ("/js/app.js", "text/javascript"),
        ("/js/api.js", "text/javascript"),
        ("/js/render.js", "text/javascript"),
        ("/js/fonts.js", "text/javascript"),
    ],
)
def test_serves_assets_with_correct_mime(server, path, mime):
    status, _, headers = get_raw(server, path)
    assert status == 200
    assert mime in headers["Content-Type"]


def test_available_fonts_returns_list():
    assert isinstance(available_fonts(), list)


def test_port_conflict_raises():
    """既に使用中のポートを指定した場合は分かりやすいエラーになる."""
    dataset = Dataset.from_payload([dict(SAMPLE)])
    first = ClockServer(build_config({"port": 0, "kiosk": False}), dataset)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="ポート"):
            ClockServer(
                build_config({"port": first.port, "kiosk": False}), dataset
            )
    finally:
        first.shutdown()
