"""literary_clock.json の読み込み・検証・時刻引き当て.

想定スキーマ (1 エントリ):
    {
      "time": "16:40",
      "excerpt": "午後四時四十分",
      "before": "...", "after": "...", "quote": "...",
      "author": "大倉燁子", "title": "深夜の客",
      "source": {"collection": ..., "author_id": ..., "file": ...}
    }

00:00〜23:50 の 10 分刻み・全 144 スロットを想定するが、
欠損スロットがあっても「直近の過去スロット」へフォールバックして動作する。
"""

from __future__ import annotations

import json
import random
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

SLOT_MINUTES = 10
SLOT_COUNT = 24 * 60 // SLOT_MINUTES  # 144

REQUIRED_FIELDS = ("time", "excerpt", "author", "title")


class DatasetError(ValueError):
    """データセットが読み込めない/不正な場合に送出される."""


def slot_index(hour: int, minute: int) -> int:
    """時刻からスロット番号 (0..143) を求める."""
    if not (0 <= hour <= 23):
        raise ValueError(f"hour が範囲外です: {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"minute が範囲外です: {minute}")
    return (hour * 60 + minute) // SLOT_MINUTES


def slot_label(index: int) -> str:
    """スロット番号から "HH:MM" 表記を作る."""
    index %= SLOT_COUNT
    total = index * SLOT_MINUTES
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_time(value: str) -> int:
    """"HH:MM" 文字列をスロット番号へ変換する."""
    if not isinstance(value, str):
        raise DatasetError(f"time は文字列で指定してください: {value!r}")
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise DatasetError(f"time は HH:MM 形式で指定してください: {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise DatasetError(f"time を数値として解釈できません: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise DatasetError(f"time が範囲外です: {value!r}")
    return slot_index(hour, minute)


def _clean(text: Any) -> str:
    """青空文庫由来のテキストを表示用に整える.

    - None/非文字列は空文字に
    - 全角スペースは維持しつつ、改行・タブは空白へ畳む
    - 前後の空白を除去
    """
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFC", text)
    for ch in ("\r\n", "\r", "\n", "\t"):
        normalized = normalized.replace(ch, " ")
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    return normalized.strip()


@dataclass(frozen=True)
class Entry:
    """時計に表示する 1 件の引用."""

    time: str
    slot: int
    excerpt: str
    before: str
    after: str
    quote: str
    author: str
    title: str
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def length(self) -> int:
        """引用文の文字数 (表示サイズの自動調整に使う)."""
        return len(self.quote)

    def to_client(self) -> dict[str, Any]:
        """フロントエンドへ渡す辞書表現."""
        return {
            "time": self.time,
            "slot": self.slot,
            "excerpt": self.excerpt,
            "before": self.before,
            "after": self.after,
            "quote": self.quote,
            "author": self.author,
            "title": self.title,
            "length": self.length,
        }

    @classmethod
    def from_raw(cls, raw: dict[str, Any], index: int) -> "Entry":
        if not isinstance(raw, dict):
            raise DatasetError(f"[{index}] エントリはオブジェクト形式にしてください")

        missing = [f for f in REQUIRED_FIELDS if not _clean(raw.get(f))]
        if missing:
            raise DatasetError(f"[{index}] 必須フィールドが欠落/空です: {', '.join(missing)}")

        slot = parse_time(raw["time"])
        excerpt = _clean(raw.get("excerpt"))
        before = _clean(raw.get("before"))
        after = _clean(raw.get("after"))

        quote = _clean(raw.get("quote"))
        if not quote:
            # quote が無い場合は before + excerpt + after から復元する
            quote = f"{before}{excerpt}{after}"

        # excerpt が quote に含まれない場合、強調表示ができないため
        # before/after から組み直した文を採用する
        if excerpt not in quote:
            rebuilt = f"{before}{excerpt}{after}"
            if excerpt in rebuilt:
                quote = rebuilt

        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}

        return cls(
            time=slot_label(slot),
            slot=slot,
            excerpt=excerpt,
            before=before,
            after=after,
            quote=quote,
            author=_clean(raw.get("author")),
            title=_clean(raw.get("title")),
            source=source,
        )


class Dataset:
    """スロット番号 → 引用エントリの索引."""

    def __init__(self, entries: Iterable[Entry]) -> None:
        self._by_slot: dict[int, list[Entry]] = {}
        count = 0
        for entry in entries:
            self._by_slot.setdefault(entry.slot, []).append(entry)
            count += 1
        if count == 0:
            raise DatasetError("データセットに有効なエントリが 1 件もありません")
        self._count = count
        self._rng = random.Random()

    # --- 基本情報 ---
    def __len__(self) -> int:
        return self._count

    @property
    def slots_filled(self) -> int:
        return len(self._by_slot)

    def missing_slots(self) -> list[str]:
        """エントリが存在しないスロットの一覧 ("HH:MM")."""
        return [slot_label(i) for i in range(SLOT_COUNT) if i not in self._by_slot]

    def candidates(self, slot: int) -> list[Entry]:
        """指定スロットのエントリ一覧 (無ければ空リスト)."""
        return list(self._by_slot.get(slot % SLOT_COUNT, ()))

    # --- 引き当て ---
    def resolve(self, slot: int) -> tuple[Entry, bool]:
        """スロットに対応するエントリを返す.

        該当が無い場合は直近の過去スロットへ遡る (最大 144 段, 日付跨ぎ可)。
        戻り値は (エントリ, 完全一致か) のタプル。
        """
        slot %= SLOT_COUNT
        for back in range(SLOT_COUNT):
            probe = (slot - back) % SLOT_COUNT
            bucket = self._by_slot.get(probe)
            if bucket:
                return bucket[0], back == 0
        raise DatasetError("エントリが見つかりません")  # pragma: no cover - __init__ で保証

    def resolve_at(self, hour: int, minute: int) -> tuple[Entry, bool]:
        """時・分からエントリを引き当てる."""
        return self.resolve(slot_index(hour, minute))

    def pick(self, slot: int, rotation: int = 0) -> tuple[Entry, bool]:
        """候補が複数ある場合に rotation 番目 (循環) を返す."""
        slot %= SLOT_COUNT
        bucket = self._by_slot.get(slot)
        if bucket:
            return bucket[rotation % len(bucket)], True
        return self.resolve(slot)

    def iter_entries(self) -> Iterator[Entry]:
        for slot in sorted(self._by_slot):
            yield from self._by_slot[slot]

    # --- 読み込み ---
    @classmethod
    def load(cls, path: str | Path, strict: bool = False) -> "Dataset":
        """JSON ファイルからデータセットを読み込む.

        strict=False の場合、壊れたエントリは警告対象として読み飛ばす。
        """
        p = Path(path)
        if not p.is_file():
            raise DatasetError(
                f"データセットが見つかりません: {p}\n"
                "  --dataset で literary_clock.json のパスを指定してください。"
            )
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise DatasetError(f"UTF-8 として読めません: {p} ({exc})") from exc
        except json.JSONDecodeError as exc:
            raise DatasetError(f"JSON の構文エラー: {p}:{exc.lineno} {exc.msg}") from exc

        return cls.from_payload(payload, strict=strict, origin=str(p))

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        strict: bool = False,
        origin: str = "<memory>",
    ) -> "Dataset":
        """JSON デコード済みのデータからデータセットを構築する."""
        # 配列直渡し / {"entries": [...]} / {"data": [...]} に対応
        if isinstance(payload, dict):
            for key in ("entries", "data", "items", "clock"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise DatasetError(f"JSON のトップレベルは配列にしてください: {origin}")

        entries: list[Entry] = []
        errors: list[str] = []
        for i, raw in enumerate(payload):
            try:
                entries.append(Entry.from_raw(raw, i))
            except DatasetError as exc:
                errors.append(str(exc))
                if strict:
                    raise

        if not entries:
            detail = "; ".join(errors[:3])
            raise DatasetError(f"有効なエントリがありません: {origin} {detail}")

        ds = cls(entries)
        ds.load_errors = errors  # type: ignore[attr-defined]
        ds.origin = origin  # type: ignore[attr-defined]
        return ds
