import json
from pathlib import Path

import telegram_gateway.runtime  # noqa: F401  (wiring must import cleanly)
from telegram_gateway.normalize import parse_update
from telegram_gateway.sticker_table import StickerTable, StickerTableInjector


def make_pack(tmp_path: Path) -> Path:
    (tmp_path / "custom_stickers.json").write_text(
        json.dumps(
            [
                {"description": "喵", "fileName": "a.png"},
                {"description": "咬你", "fileName": "b.png"},
                {"description": "咬你~", "fileName": "c.png"},
            ]
        ),
        encoding="utf-8",
    )
    # the transcoder renames .png -> .webp; load() must stem-match across that
    for name in ("a.webp", "b.webp", "c.webp"):
        (tmp_path / name).write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 22)
    return tmp_path


def test_load_stem_matches_renamed_files(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    assert table.names == ["喵", "咬你", "咬你~"]
    entry = table.resolve("喵")
    assert entry is not None and entry.file_path is not None
    assert entry.file_path.name == "a.webp"


def test_resolve_exact_collision_and_unknown(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    assert table.resolve("咬你").name == "咬你"
    assert table.resolve("咬你~").name == "咬你~"
    assert table.resolve(" 咬你~ ").name == "咬你~"
    assert table.resolve("完全不相干的一句话") is None


def test_segment_preserves_order_and_drops_unknown_marks(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    parts = table.segment("在想你\n[表情包：喵]\n哈哈\n[表情包：自造名]\n结尾")
    assert [p.kind for p in parts] == ["text", "sticker", "text", "text"]
    assert parts[0].text == "在想你"
    assert parts[1].entry.name == "喵"
    assert parts[2].text == "哈哈"
    assert parts[3].text == "结尾"


def test_segment_plain_text_without_marks(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    parts = table.segment("就是普通的一句话")
    assert [(p.kind, p.text) for p in parts] == [("text", "就是普通的一句话")]


def test_injector_once_per_session(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    injector = StickerTableInjector(table)
    first = injector.decorate("tg:1", "在吗")
    second = injector.decorate("tg:1", "在的")
    other = injector.decorate("tg:2", "在吗")
    assert "【表情包表" in first and "【表情包表" not in second and "【表情包表" in other


def test_injector_off_and_missing_table(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    assert StickerTableInjector(table, mode="off").decorate("tg:1", "hi") == "hi"
    assert StickerTableInjector(None).decorate("tg:1", "hi") == "hi"


def test_inbound_sticker_becomes_marker():
    update = {
        "@type": "updateNewMessage",
        "message": {
            "chat_id": 42,
            "id": 7,
            "is_outgoing": False,
            "content": {"@type": "messageSticker", "sticker": {"emoji": "\U0001f431"}},
        },
    }
    message = parse_update(update)
    assert message is not None
    assert message.content_type == "text"
    assert message.text == "[用户发了一个表情包：\U0001f431]"


def test_bare_bracket_mark_resolves_to_sticker(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    parts = table.segment("好\n[咬你]\n好")
    assert [p.kind for p in parts] == ["text", "sticker", "text"]
    assert parts[1].entry.name == "咬你"


def test_plain_bracket_text_stays_text(tmp_path):
    table = StickerTable.load(make_pack(tmp_path))
    parts = table.segment("[撤回一条消息]\n[语音消息]")
    assert [p.kind for p in parts] == ["text", "text"]
