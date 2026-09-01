#!/usr/bin/env python3
"""動作確認用のサンプルデータセットを生成する.

本物の `literary_clock.json` (青空文庫コーパス由来) が手元に無くても
アプリの表示・アニメーション・レイアウトを確認できるようにするための
プレースホルダを作る。

重要:
  実在の作家名・作品名は使用しない。author / title は
  「（サンプル）」と明示し、本物のデータと混同しないようにしている。
  本番では必ず実際の literary_clock.json を使用すること。

使い方:
    python3 scripts/make_sample_dataset.py -o data/literary_clock.sample.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

KANJI = "〇一二三四五六七八九"
KANJI_10 = ["", "十", "二十", "三十", "四十", "五十"]


def kanji_number(n: int) -> str:
    """0..59 を漢数字にする (十/二十…表記)."""
    if n < 10:
        return KANJI[n]
    tens, ones = divmod(n, 10)
    return KANJI_10[tens] + (KANJI[ones] if ones else "")


def japanese_time(hour: int, minute: int) -> str:
    """「午後四時四十分」のような時刻表現を作る."""
    if hour == 0:
        period, h12 = "午前", 12
    elif hour < 12:
        period, h12 = "午前", hour
    elif hour == 12:
        period, h12 = "午後", 12
    else:
        period, h12 = "午後", hour - 12

    text = f"{period}{kanji_number(h12)}時"
    if minute:
        text += f"{kanji_number(minute)}分"
    else:
        text += "ちょうど"
    return text


# 時間帯ごとの前後文脈 (雰囲気づけ用のサンプル文)
CONTEXTS = {
    "deep_night": (
        ["柱時計の音だけが家中に響いていて、", "眠れぬまま窓の外を見ていると、", "灯を落とした部屋で耳を澄ますと、"],
        ["に、遠くで貨物列車が過ぎて行った。", "を告げる鐘が、雪のなかで鈍く鳴った。", "の静けさは、水底のようであった。"],
    ),
    "dawn": (
        ["障子がうすあかるくなって、", "鳥の声で目をさましたのは、", "宿の者が起き出す気配がして、"],
        ["には、もう霧が谷を降りていた。", "の空は、藍から白へ移ろうところだった。", "を待って、私は草履をはいた。"],
    ),
    "morning": (
        ["約束を思い出して急に立ち上がったのは、", "郵便が届いたのは、", "駅の改札を抜けたときには、"],
        ["を少し過ぎたころであった。", "の日ざしが、机の端まで届いていた。", "発の汽車に間に合うはずだった。"],
    ),
    "noon": (
        ["工場の笛が鳴り渡って、", "縁側で新聞をひろげたまま、", "客の絶えた店先で、"],
        ["を知らせた。", "になったのにも気づかなかった。", "の暑さは、石畳を白く焼いていた。"],
    ),
    "afternoon": (
        ["で、ちょっと行き渋ったが、断わる理由もないと思い、", "手紙を書き終えたのは、", "坂を下りきったところで振り返ると、"],
        ["発の急行で、東京駅を立つことにした。", "の鐘が、寺の方から聞こえてきた。", "の影が、長く伸びはじめていた。"],
    ),
    "evening": (
        ["湯屋から戻る道で、", "帳場の柱時計を見上げると、", "洋燈に火を入れたのは、"],
        ["の空が、まだ赤みを残していた。", "を指していた。", "を告げる声が、路地の奥から流れてきた。"],
    ),
    "night": (
        ["雨戸を閉めようとして、", "客はとうに帰り、", "机の上の原稿を片づけたのは、"],
        ["の風が思いのほか冷たいのに驚いた。", "の柱時計が二つ三つ鳴った。", "を回っていた。"],
    ),
}


def band(hour: int) -> str:
    if hour < 4:
        return "deep_night"
    if hour < 6:
        return "dawn"
    if hour < 11:
        return "morning"
    if hour < 13:
        return "noon"
    if hour < 17:
        return "afternoon"
    if hour < 20:
        return "evening"
    return "night"


def build() -> list[dict]:
    entries = []
    for slot in range(144):
        total = slot * 10
        hour, minute = divmod(total, 60)
        befores, afters = CONTEXTS[band(hour)]
        before = befores[slot % len(befores)]
        after = afters[(slot // 3) % len(afters)]
        excerpt = japanese_time(hour, minute)

        entries.append(
            {
                "time": f"{hour:02d}:{minute:02d}",
                "excerpt": excerpt,
                "before": before,
                "after": after,
                "quote": f"{before}{excerpt}{after}",
                "author": "（サンプル）",
                "title": "（サンプルデータ・実データに置き換えてください）",
                "source": {
                    "collection": "sample_placeholder",
                    "author_id": "000000",
                    "file": "sample/placeholder.txt",
                },
            }
        )
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description="サンプルデータセットを生成する")
    ap.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("data/literary_clock.sample.json"),
        help="出力先 (既定: data/literary_clock.sample.json)",
    )
    args = ap.parse_args()

    entries = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{len(entries)} 件のサンプルを書き出しました: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
