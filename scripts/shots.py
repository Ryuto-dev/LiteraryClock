#!/usr/bin/env python3
"""開発用: 各テーマ / 組み方向のスクリーンショットを撮る.

使い方:
    python3 scripts/shots.py --url http://127.0.0.1:8730 --out /tmp/shots
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

CASES = [
    ("ink-h", "ink", "horizontal"),
    ("washi-h", "washi", "horizontal"),
    ("night-h", "night", "horizontal"),
    ("sepia-h", "sepia", "horizontal"),
    ("mono-h", "mono", "horizontal"),
    ("ink-v", "ink", "vertical"),
    ("washi-v", "washi", "vertical"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8730")
    ap.add_argument("--out", type=Path, default=Path("/tmp/shots"))
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
        )
        await page.goto(args.url, wait_until="networkidle")
        await page.wait_for_selector('body[data-state="ready"]', timeout=15000)
        await page.wait_for_timeout(2500)  # 入場アニメーションの完了を待つ

        for name, theme, writing in CASES:
            await page.evaluate(
                """([theme, writing]) => {
                    document.body.dataset.theme = theme;
                    document.body.dataset.writing = writing;
                }""",
                [theme, writing],
            )
            await page.wait_for_timeout(1200)
            path = args.out / f"{name}.png"
            await page.screenshot(path=str(path))
            print(f"撮影: {path}")

        # 長文 (自動縮小の確認) と短文
        for label, time in (("long", "23:50"), ("short", "12:00")):
            await page.evaluate(
                """(t) => { document.body.dataset.theme='ink';
                            document.body.dataset.writing='horizontal'; }""",
                time,
            )
            await page.wait_for_timeout(400)

        # 7 インチ公式タッチパネル相当
        await page.set_viewport_size({"width": 800, "height": 480})
        await page.evaluate(
            """() => { document.body.dataset.theme='ink';
                       document.body.dataset.writing='horizontal'; }"""
        )
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(args.out / "pi7inch.png"))
        print(f"撮影: {args.out / 'pi7inch.png'}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
