"""dataset モジュールのテスト."""

from __future__ import annotations

import json

import pytest

from literaryclock.dataset import (
    SLOT_COUNT,
    Dataset,
    DatasetError,
    Entry,
    parse_time,
    slot_index,
    slot_label,
)


# --------------------------------------------------------------------------
# 時刻 <-> スロット変換
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (0, 0, 0),
        (0, 9, 0),      # 端数は切り捨て (同一スロット)
        (0, 10, 1),
        (12, 0, 72),
        (16, 40, 100),
        (16, 45, 100),  # 45 分は 40 分スロット
        (23, 50, 143),
        (23, 59, 143),
    ],
)
def test_slot_index(hour, minute, expected):
    assert slot_index(hour, minute) == expected


def test_slot_index_rejects_out_of_range():
    with pytest.raises(ValueError):
        slot_index(24, 0)
    with pytest.raises(ValueError):
        slot_index(0, 60)


@pytest.mark.parametrize(
    "index,expected",
    [(0, "00:00"), (1, "00:10"), (100, "16:40"), (143, "23:50")],
)
def test_slot_label(index, expected):
    assert slot_label(index) == expected


def test_slot_label_wraps():
    assert slot_label(SLOT_COUNT) == "00:00"


@pytest.mark.parametrize(
    "text,expected",
    [("00:00", 0), ("16:40", 100), (" 16:40 ", 100), ("23:50", 143), ("9:30", 57)],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["", "16", "16:60", "24:00", "abc", "16:40:00"])
def test_parse_time_rejects_invalid(bad):
    with pytest.raises(DatasetError):
        parse_time(bad)


def test_parse_time_accepts_fullwidth_digits():
    """全角数字も受け付ける (日本語データセットでの混在に備えた寛容さ)."""
    assert parse_time("１６:４０") == 100


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------
SAMPLE = {
    "time": "16:40",
    "excerpt": "午後四時四十分",
    "before": "で、ちょっと行き渋ったが、",
    "after": "発の急行で、東京駅を立ったのだった。",
    "quote": "で、ちょっと行き渋ったが、午後四時四十分発の急行で、東京駅を立ったのだった。",
    "author": "大倉燁子",
    "title": "深夜の客",
    "source": {"collection": "aozorabunko_text", "author_id": "001669"},
}


def test_entry_from_raw():
    e = Entry.from_raw(SAMPLE, 0)
    assert e.time == "16:40"
    assert e.slot == 100
    assert e.excerpt == "午後四時四十分"
    assert e.author == "大倉燁子"
    assert e.title == "深夜の客"
    assert e.source["author_id"] == "001669"
    assert e.length == len(SAMPLE["quote"])


def test_entry_rebuilds_quote_when_missing():
    """quote が無い場合 before + excerpt + after から復元する."""
    raw = {k: v for k, v in SAMPLE.items() if k != "quote"}
    e = Entry.from_raw(raw, 0)
    assert e.quote == SAMPLE["quote"]


def test_entry_rebuilds_quote_when_excerpt_absent():
    """quote に excerpt が含まれない場合は組み直す (強調表示のため)."""
    raw = dict(SAMPLE, quote="まったく無関係な文章です。")
    e = Entry.from_raw(raw, 0)
    assert e.excerpt in e.quote


def test_entry_normalizes_newlines():
    raw = dict(SAMPLE, before="改行\nを含む\r\n文脈")
    e = Entry.from_raw(raw, 0)
    assert "\n" not in e.before
    assert "\r" not in e.before


@pytest.mark.parametrize("field", ["time", "excerpt", "author", "title"])
def test_entry_requires_fields(field):
    raw = dict(SAMPLE)
    raw[field] = ""
    with pytest.raises(DatasetError):
        Entry.from_raw(raw, 0)


def test_entry_rejects_non_dict():
    with pytest.raises(DatasetError):
        Entry.from_raw("文字列", 0)  # type: ignore[arg-type]


def test_entry_to_client_has_display_fields():
    payload = Entry.from_raw(SAMPLE, 0).to_client()
    for key in ("time", "slot", "excerpt", "before", "after", "quote",
                "author", "title", "length"):
        assert key in payload


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
def make_full_payload():
    """144 スロットすべてを埋めたペイロードを作る."""
    out = []
    for i in range(SLOT_COUNT):
        out.append(
            dict(
                SAMPLE,
                time=slot_label(i),
                excerpt=f"時刻{i}",
                quote=f"文脈{i}時刻{i}文脈",
                before=f"文脈{i}",
                after="文脈",
            )
        )
    return out


def test_dataset_full_has_no_missing_slots():
    ds = Dataset.from_payload(make_full_payload())
    assert len(ds) == SLOT_COUNT
    assert ds.slots_filled == SLOT_COUNT
    assert ds.missing_slots() == []


def test_dataset_resolve_exact():
    ds = Dataset.from_payload(make_full_payload())
    entry, exact = ds.resolve(100)
    assert exact is True
    assert entry.time == "16:40"


def test_dataset_falls_back_to_earlier_slot():
    """欠損スロットは直近の過去スロットで代替される."""
    ds = Dataset.from_payload([dict(SAMPLE, time="16:40")])
    entry, exact = ds.resolve(parse_time("17:30"))
    assert entry.time == "16:40"
    assert exact is False


def test_dataset_fallback_wraps_across_midnight():
    """00:10 で 23:50 のエントリしか無い場合、日付を跨いで遡る."""
    ds = Dataset.from_payload([dict(SAMPLE, time="23:50")])
    entry, exact = ds.resolve(parse_time("00:10"))
    assert entry.time == "23:50"
    assert exact is False


def test_dataset_resolve_at():
    ds = Dataset.from_payload(make_full_payload())
    entry, exact = ds.resolve_at(16, 45)
    assert entry.time == "16:40"
    assert exact is True


def test_dataset_multiple_candidates_rotate():
    payload = [
        dict(SAMPLE, time="16:40", quote="一つ目の午後四時四十分です", excerpt="午後四時四十分"),
        dict(SAMPLE, time="16:40", quote="二つ目の午後四時四十分です", excerpt="午後四時四十分"),
    ]
    ds = Dataset.from_payload(payload)
    assert len(ds.candidates(100)) == 2
    first, _ = ds.pick(100, 0)
    second, _ = ds.pick(100, 1)
    third, _ = ds.pick(100, 2)  # 循環して 1 件目へ戻る
    assert first.quote != second.quote
    assert third.quote == first.quote


def test_dataset_skips_broken_entries_by_default():
    payload = [dict(SAMPLE), {"time": "bad"}, dict(SAMPLE, time="00:00")]
    ds = Dataset.from_payload(payload)
    assert len(ds) == 2
    assert len(ds.load_errors) == 1


def test_dataset_strict_raises_on_broken_entry():
    payload = [dict(SAMPLE), {"time": "bad"}]
    with pytest.raises(DatasetError):
        Dataset.from_payload(payload, strict=True)


def test_dataset_rejects_empty():
    with pytest.raises(DatasetError):
        Dataset.from_payload([])


def test_dataset_rejects_non_list():
    with pytest.raises(DatasetError):
        Dataset.from_payload({"foo": "bar"})


def test_dataset_accepts_wrapped_payload():
    """{"entries": [...]} 形式も受け付ける."""
    ds = Dataset.from_payload({"entries": [dict(SAMPLE)]})
    assert len(ds) == 1


# --------------------------------------------------------------------------
# ファイル読み込み
# --------------------------------------------------------------------------
def test_dataset_load_from_file(tmp_path):
    path = tmp_path / "clock.json"
    path.write_text(json.dumps(make_full_payload(), ensure_ascii=False), encoding="utf-8")
    ds = Dataset.load(path)
    assert len(ds) == SLOT_COUNT


def test_dataset_load_missing_file(tmp_path):
    with pytest.raises(DatasetError, match="見つかりません"):
        Dataset.load(tmp_path / "nope.json")


def test_dataset_load_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="構文エラー"):
        Dataset.load(path)


def test_dataset_iter_entries_sorted_by_slot():
    payload = [dict(SAMPLE, time="23:50"), dict(SAMPLE, time="00:00")]
    ds = Dataset.from_payload(payload)
    slots = [e.slot for e in ds.iter_entries()]
    assert slots == sorted(slots)
