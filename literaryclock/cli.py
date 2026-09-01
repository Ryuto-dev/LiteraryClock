"""コマンドラインインタフェース.

  literary-clock                  … 全画面で時計を起動
  literary-clock --no-kiosk       … サーバのみ (ブラウザは手動で開く)
  literary-clock --monitor 1      … 2 番目の HDMI 出力に全画面表示
  literary-clock monitors         … 接続中のディスプレイ一覧を表示
  literary-clock validate <file>  … データセットを検証
  literary-clock preview 16:40    … 指定時刻の引用を端末に表示
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import DEFAULTS, THEMES, TRANSITIONS, WRITING_MODES, ConfigError, build_config
from .dataset import SLOT_COUNT, Dataset, DatasetError, parse_time, slot_index
from .display import (
    disable_screen_blanking,
    hide_cursor,
    launch_kiosk,
    list_monitors_text,
    select_monitor,
)
from .monitors import MonitorError
from .server import ClockServer

log = logging.getLogger("literaryclock")


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="literary-clock",
        description="青空文庫コーパスによる文学時計 (Raspberry Pi 向け全画面表示)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  literary-clock --dataset data/literary_clock.json\n"
            "  literary-clock --theme washi --writing-mode vertical\n"
            "  literary-clock --no-kiosk --host 0.0.0.0 --port 8730\n"
            "  literary-clock monitors                # ディスプレイ一覧\n"
            "  literary-clock --monitor 1             # 2 番目の画面に表示\n"
            "  literary-clock --monitor HDMI-2        # コネクタ名で指定\n"
            "  literary-clock --monitor right         # 右側の画面に表示\n"
            "  literary-clock validate data/literary_clock.json\n"
            "  literary-clock preview 16:40\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"LiteraryClock {__version__}")

    # データ / サーバ
    g = p.add_argument_group("データとサーバ")
    g.add_argument("--dataset", metavar="PATH", help="literary_clock.json のパス")
    g.add_argument("--config", metavar="PATH", type=Path, help="設定ファイル (JSON)")
    g.add_argument("--host", help=f"待受ホスト (既定: {DEFAULTS['host']})")
    g.add_argument("--port", type=int, help=f"待受ポート (既定: {DEFAULTS['port']})")

    # 表示
    d = p.add_argument_group("表示")
    d.add_argument("--theme", choices=THEMES, help="配色テーマ")
    d.add_argument("--writing-mode", dest="writing_mode", choices=WRITING_MODES,
                   help="組み方向 (horizontal=横書き / vertical=縦書き / auto)")
    d.add_argument("--transition", choices=TRANSITIONS, help="切替アニメーション")
    d.add_argument("--font-scale", dest="font_scale", type=float,
                   help="文字サイズ倍率 (0.4〜3.0)")
    d.add_argument("--rotate-seconds", dest="rotate_seconds", type=int,
                   help="同一時刻に複数候補がある場合の切替間隔 (秒, 0で無効)")
    d.add_argument("--no-credit", dest="show_credit", action="store_false", default=None,
                   help="作者・作品名を表示しない")
    d.add_argument("--no-digital-clock", dest="show_digital_clock", action="store_false",
                   default=None, help="デジタル時刻を表示しない")
    d.add_argument("--no-progress", dest="show_progress", action="store_false", default=None,
                   help="進捗インジケータを表示しない")
    d.add_argument("--no-highlight", dest="highlight_excerpt", action="store_false",
                   default=None, help="時刻部分を強調表示しない")

    # ディスプレイ (Raspberry Pi は HDMI 2 系統)
    m = p.add_argument_group("ディスプレイ (マルチモニタ)")
    m.add_argument("--monitor", metavar="SPEC",
                   help="表示先の画面。番号 (0,1) / コネクタ名 (HDMI-1, HDMI-A-2) / "
                        "primary,left,right,top,bottom / モニタ名の一部")
    m.add_argument("--list-monitors", action="store_true",
                   help="接続中のディスプレイを一覧表示して終了する")
    m.add_argument("--strict-monitor", dest="monitor_fallback", action="store_false",
                   default=None,
                   help="--monitor が見つからない場合にプライマリへ逃げずエラー終了する")
    m.add_argument("--window-position", dest="window_position", metavar="X,Y",
                   help="ウィンドウ位置を直接指定 (--monitor より優先)")
    m.add_argument("--window-size", dest="window_size", metavar="W,H",
                   help="ウィンドウサイズを直接指定 (--monitor より優先)")

    # 動作
    r = p.add_argument_group("動作")
    r.add_argument("--no-kiosk", dest="kiosk", action="store_false", default=None,
                   help="ブラウザを起動せずサーバのみ動かす")
    r.add_argument("--browser", help="使用するブラウザ実行ファイル")
    r.add_argument("--fake-time", dest="fake_time", metavar="HH:MM",
                   help="時刻を固定する (デバッグ用)")
    r.add_argument("--time-speed", dest="time_speed", type=float,
                   help="時間の進みを N 倍速にする (デモ用)")
    r.add_argument("--strict", action="store_true",
                   help="データセットの不正エントリでエラー終了する")
    r.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")
    r.add_argument("-q", "--quiet", action="store_true", help="警告以上のみ表示")

    sub = p.add_subparsers(dest="command")

    v = sub.add_parser("validate", help="データセットを検証する")
    v.add_argument("path", nargs="?", help="literary_clock.json のパス")
    v.add_argument("--strict", action="store_true", help="不正エントリでエラー終了")
    v.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")

    pv = sub.add_parser("preview", help="指定時刻の引用を端末に表示する")
    pv.add_argument("time", nargs="?", help="HH:MM (省略時は現在時刻)")
    pv.add_argument("--dataset", help="literary_clock.json のパス")
    pv.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")

    mo = sub.add_parser("monitors", help="接続中のディスプレイを一覧表示する")
    mo.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")

    return p


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    keys = (
        "dataset", "host", "port", "theme", "writing_mode", "transition",
        "font_scale", "rotate_seconds", "show_credit", "show_digital_clock",
        "show_progress", "highlight_excerpt", "kiosk", "browser",
        "fake_time", "time_speed",
        "monitor", "monitor_fallback", "window_position", "window_size",
    )
    return {k: getattr(args, k, None) for k in keys}


def _load_dataset(path: str, strict: bool) -> Dataset:
    ds = Dataset.load(path, strict=strict)
    errors = getattr(ds, "load_errors", [])
    if errors:
        log.warning("読み飛ばした不正エントリ: %d 件", len(errors))
        for msg in errors[:5]:
            log.warning("  %s", msg)
        if len(errors) > 5:
            log.warning("  ... 他 %d 件", len(errors) - 5)
    return ds


# --------------------------------------------------------------------------
# サブコマンド
# --------------------------------------------------------------------------
def cmd_validate(args: argparse.Namespace) -> int:
    path = args.path or DEFAULTS["dataset"]
    try:
        ds = _load_dataset(path, strict=args.strict)
    except DatasetError as exc:
        print(f"NG: {exc}", file=sys.stderr)
        return 1

    missing = ds.missing_slots()
    filled = ds.slots_filled
    print(f"データセット : {path}")
    print(f"エントリ数   : {len(ds)}")
    print(f"充填スロット : {filled} / {SLOT_COUNT}  ({filled / SLOT_COUNT * 100:.1f}%)")

    errors = getattr(ds, "load_errors", [])
    if errors:
        print(f"不正エントリ : {len(errors)} 件 (読み飛ばし)")

    if missing:
        print(f"欠損スロット : {len(missing)} 件")
        preview = ", ".join(missing[:12])
        print(f"  {preview}{' ...' if len(missing) > 12 else ''}")
        print("  → 欠損時は直近の過去スロットの引用で代替表示します。")
    else:
        print("欠損スロット : なし (全 144 スロット充填)")

    # 長すぎる引用は表示が小さくなるため警告
    long_entries = [e for e in ds.iter_entries() if e.length > 220]
    if long_entries:
        print(f"長文の引用   : {len(long_entries)} 件 (220 文字超, 自動縮小されます)")

    no_highlight = [e for e in ds.iter_entries() if e.excerpt not in e.quote]
    if no_highlight:
        print(f"注意         : excerpt が quote に含まれない項目 {len(no_highlight)} 件")

    print("\nOK: データセットは利用可能です。")
    return 0


def cmd_monitors(_args: argparse.Namespace) -> int:
    """接続中のディスプレイを一覧表示する."""
    print(list_monitors_text())
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    path = args.dataset or DEFAULTS["dataset"]
    try:
        ds = _load_dataset(path, strict=False)
    except DatasetError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    if args.time:
        try:
            slot = parse_time(args.time)
        except DatasetError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            return 1
    else:
        now = datetime.now()
        slot = slot_index(now.hour, now.minute)

    entry, exact = ds.resolve(slot)
    bar = "─" * 56
    print(bar)
    print(f"  {entry.time}{'' if exact else '  (直近スロットで代替)'}")
    print(bar)
    print()
    for line in _wrap(entry.quote, 50):
        print(f"  {line}")
    print()
    print(f"  ── {entry.author}『{entry.title}』")
    print(bar)
    return 0


def _wrap(text: str, width: int) -> list[str]:
    """日本語向けの素朴な折り返し (文字数ベース)."""
    return [text[i : i + width] for i in range(0, len(text), width)] or [""]


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = build_config(_cli_overrides(args), config_path=args.config)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    try:
        dataset = _load_dataset(config.dataset, strict=args.strict)
    except DatasetError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filled = dataset.slots_filled
    log.info(
        "データセット: %s (%d 件 / %d スロット充填)",
        config.dataset, len(dataset), filled,
    )
    if filled < SLOT_COUNT:
        log.warning(
            "%d スロットが欠損しています (直近の過去スロットで代替表示)",
            SLOT_COUNT - filled,
        )

    # ブラウザ起動前にモニタを解決しておく
    # (--strict-monitor 時はサーバを立てる前に失敗させたい)
    monitor = None
    if config.kiosk:
        try:
            monitor = select_monitor(
                config.get("monitor", ""),
                fallback=bool(config.get("monitor_fallback", True)),
            )
        except MonitorError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            return 2

    try:
        server = ClockServer(config, dataset)
    except RuntimeError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    server.start()
    if not server.wait_ready():
        log.warning("サーバの応答確認がタイムアウトしました (継続します)")

    print(f"\n  文学時計を起動しました → {server.url}")
    print("  終了するには Ctrl+C を押してください。\n")

    browser_proc = None
    cursor_proc = None
    if config.kiosk:
        if config.disable_blanking:
            disable_screen_blanking()
        if config.hide_cursor:
            cursor_proc = hide_cursor()
        browser_proc = launch_kiosk(
            server.url,
            browser=config.browser,
            monitor=monitor,
            window_position=config.get("window_position", ""),
            window_size=config.get("window_size", ""),
        )
        if browser_proc is None:
            log.warning("kiosk 起動に失敗しました。ブラウザで上記 URL を開いてください。")

    stop = signal.SIGTERM

    def _on_signal(signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = signum
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        # ブラウザが終了したら時計も終了する (kiosk 運用時)
        while True:
            if browser_proc is not None and browser_proc.poll() is not None:
                log.info("ブラウザが終了したため停止します")
                break
            import time as _time

            _time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("終了シグナルを受け取りました")
    finally:
        for proc in (browser_proc, cursor_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:  # pragma: no cover - 強制終了
                    proc.kill()
        server.shutdown()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False), getattr(args, "quiet", False))

    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "monitors":
        return cmd_monitors(args)
    if getattr(args, "list_monitors", False):
        return cmd_monitors(args)
    return cmd_run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
