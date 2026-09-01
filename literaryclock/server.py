"""ローカル HTTP サーバ.

フロントエンド (web/) の静的配信と、以下の JSON API を提供する:

  GET /api/bootstrap        起動時設定 + 現在のエントリ
  GET /api/now              現在時刻のエントリ
  GET /api/at?time=HH:MM    指定時刻のエントリ (プリフェッチ/プレビュー用)
  GET /api/health           稼働確認

localhost 専用の想定であり、外部公開用の堅牢化はしていない。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config
from .dataset import Dataset, SLOT_COUNT, parse_time, slot_index

log = logging.getLogger("literaryclock.server")

WEB_ROOT = Path(__file__).resolve().parent / "web"

# 静的ファイルのキャッシュ設定 (フォントは長期, HTML/JS は都度検証)
_CACHE_RULES = (
    (".woff2", "public, max-age=31536000, immutable"),
    (".woff", "public, max-age=31536000, immutable"),
    (".ttf", "public, max-age=31536000, immutable"),
    (".otf", "public, max-age=31536000, immutable"),
    (".png", "public, max-age=86400"),
    (".svg", "public, max-age=86400"),
)

# 任意 Web フォントの探索対象 (web/fonts/ に置かれていれば使用する)
OPTIONAL_FONT_FILES = (
    "ZenOldMincho-Regular.woff2",
    "ZenOldMincho-Bold.woff2",
    "NotoSerifJP-Regular.woff2",
    "NotoSerifJP-SemiBold.woff2",
    "JetBrainsMono-Regular.woff2",
)


def available_fonts() -> list[str]:
    """web/fonts/ に実際に存在するフォントファイル名を返す.

    クライアントが 404 を出しながら探索しなくて済むよう、
    サーバ側で存在するものだけを列挙して伝える。
    """
    font_dir = WEB_ROOT / "fonts"
    if not font_dir.is_dir():
        return []
    return [name for name in OPTIONAL_FONT_FILES if (font_dir / name).is_file()]


class ClockHandler(SimpleHTTPRequestHandler):
    """静的配信 + 時計 API."""

    server_version = f"LiteraryClock/{__version__}"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, config: Config, dataset: Dataset, **kwargs: Any) -> None:
        self.config = config
        self.dataset = dataset
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    # --- ログを抑制 (kiosk 運用でコンソールを汚さない) ---
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        log.warning("%s - %s", self.address_string(), fmt % args)

    # --- ヘルパ ---
    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _current_slot(self) -> int:
        """設定 (fake_time / time_speed) を考慮した現在スロット."""
        cfg = self.config
        fake = cfg.get("fake_time") or ""
        if fake:
            hh, mm = (int(x) for x in fake.split(":"))
            return slot_index(hh, mm)
        now = datetime.now()
        base = slot_index(now.hour, now.minute)
        speed = float(cfg.get("time_speed", 1.0) or 1.0)
        if speed != 1.0:
            # デモ用: 経過秒を N 倍してスロットを進める
            elapsed = now.hour * 3600 + now.minute * 60 + now.second
            base = int(elapsed * speed // (10 * 60)) % SLOT_COUNT
        return base

    def _entry_payload(self, slot: int, rotation: int = 0) -> dict[str, Any]:
        entry, exact = self.dataset.pick(slot, rotation)
        payload = entry.to_client()
        payload["exact"] = exact
        payload["requested"] = slot
        payload["candidates"] = len(self.dataset.candidates(slot))
        return payload

    # --- ルーティング ---
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の規約
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route.startswith("/api"):
            try:
                self._handle_api(route, parse_qs(parsed.query))
            except ValueError as exc:
                # DatasetError は ValueError のサブクラス。
                # API 経由で発生する DatasetError は parse_time() 由来の
                # 「クライアントが渡した time パラメータが不正」のケースのみ
                # (データセット自体は起動時に検証済みで空になり得ない)。
                # そのため両方とも 400 (Bad Request) として扱う。
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # pragma: no cover - 想定外の内部エラー
                log.exception("API 処理中に予期しないエラー: %s", route)
                self._send_json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if urlparse(self.path).path.startswith("/api"):
            self.do_GET()
            return
        super().do_HEAD()

    def _handle_api(self, route: str, query: dict[str, list[str]]) -> None:
        if route == "/api/health":
            self._send_json({"ok": True, "version": __version__})
            return

        if route == "/api/fonts":
            self._send_json({"available": available_fonts()})
            return

        if route == "/api/bootstrap":
            self._send_json(
                {
                    "version": __version__,
                    "settings": self.config.client_settings(),
                    "dataset": {
                        "entries": len(self.dataset),
                        "slots_filled": self.dataset.slots_filled,
                        "slots_total": SLOT_COUNT,
                        "missing": self.dataset.missing_slots(),
                    },
                    "entry": self._entry_payload(self._current_slot()),
                }
            )
            return

        if route == "/api/now":
            rotation = int((query.get("rotation") or ["0"])[0] or 0)
            self._send_json(self._entry_payload(self._current_slot(), rotation))
            return

        if route == "/api/at":
            raw = (query.get("time") or [""])[0]
            if not raw:
                raise ValueError("time パラメータが必要です (HH:MM)")
            rotation = int((query.get("rotation") or ["0"])[0] or 0)
            self._send_json(self._entry_payload(parse_time(raw), rotation))
            return

        self._send_json({"error": f"unknown endpoint: {route}"}, HTTPStatus.NOT_FOUND)

    # --- 静的ファイルのヘッダ調整 ---
    def end_headers(self) -> None:
        path_lower = urlparse(self.path).path.lower()
        if not path_lower.startswith("/api"):
            for suffix, value in _CACHE_RULES:
                if path_lower.endswith(suffix):
                    self.send_header("Cache-Control", value)
                    break
            else:
                self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def guess_type(self, path: str) -> str:  # type: ignore[override]
        mapping = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".ttf": "font/ttf",
            ".otf": "font/otf",
        }
        for suffix, mime in mapping.items():
            if str(path).lower().endswith(suffix):
                return mime
        return super().guess_type(path)


class ClockServer:
    """バックグラウンドスレッドで動く HTTP サーバのラッパ."""

    def __init__(self, config: Config, dataset: Dataset) -> None:
        self.config = config
        self.dataset = dataset
        handler = partial(ClockHandler, config=config, dataset=dataset)

        port = int(config.port)
        host = str(config.host)
        try:
            self._httpd = ThreadingHTTPServer((host, port), handler)  # type: ignore[arg-type]
        except OSError as exc:
            raise RuntimeError(
                f"ポート {port} を開けませんでした ({exc}). "
                "--port で別のポートを指定してください。"
            ) from exc
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self.config.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="literaryclock-http",
            daemon=True,
        )
        self._thread.start()
        log.info("HTTP サーバを起動しました: %s", self.url)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """ポートが接続を受け付けるまで待つ."""
        host = self.config.host if self.config.host not in ("0.0.0.0", "::") else "127.0.0.1"
        deadline = timeout
        step = 0.05
        while deadline > 0:
            try:
                with socket.create_connection((host, self.port), timeout=0.5):
                    return True
            except OSError:
                threading.Event().wait(step)
                deadline -= step
        return False

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
        log.info("HTTP サーバを停止しました")

    def __enter__(self) -> "ClockServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()
