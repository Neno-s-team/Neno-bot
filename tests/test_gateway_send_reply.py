"""Gateway reply dispatch tests. Pure mocks: no LLM calls, no token cost."""

import asyncio
from types import SimpleNamespace

import pytest

import telegram_gateway.runtime as runtime


class FakeTdlib:
    def __init__(self):
        self.requests = []

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        return {"@type": "message", "id": len(self.requests)}

    async def wait_message_send_result(self, message_id, timeout=None):
        return {"@type": "updateMessageSendSucceeded", "old_message_id": message_id}


class FakeCore:
    def __init__(self, *args, **kwargs):
        pass

    async def close(self):
        pass


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "TdlibClient", lambda *a, **k: FakeTdlib())
    monkeypatch.setattr(runtime, "NenoCoreClient", FakeCore)
    monkeypatch.setattr(runtime.asyncio, "sleep", lambda *_a, **_k: _fake_sleep())

    sticker = tmp_path / "a.webp"
    sticker.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 22)
    (tmp_path / "custom_stickers.json").write_text(
        '[{"description": "喵", "fileName": "a.png"}]', encoding="utf-8"
    )

    config = SimpleNamespace(
        sidecar_binary=tmp_path / "sidecar.exe",
        tdjson_library=tmp_path / "tdjson.dll",
        core_url="http://127.0.0.1:9",
        internal_token="",
        sticker_pack_dir=tmp_path,
    )
    gw = runtime.TelegramGateway(config)
    assert gw.stickers is not None, "gateway must hold the sticker table for _send_reply"
    return gw


async def _fake_sleep():
    return None


def test_send_reply_dispatches_text_and_sticker_in_order(gateway):
    asyncio.run(gateway._send_reply(42, "在想你\n[表情包：喵]\n嘻嘻"))
    sent = gateway.tdlib.requests
    kinds = [r["input_message_content"]["@type"] for r in sent]
    assert kinds == ["inputMessageText", "inputMessageSticker"]
    assert sent[0]["input_message_content"]["text"]["text"] == "在想你\n嘻嘻"
    assert sent[0]["chat_id"] == 42
    assert "[表情包" not in sent[0]["input_message_content"]["text"]["text"]


def test_send_reply_unresolved_mark_is_dropped(gateway):
    asyncio.run(gateway._send_reply(42, "前\n[表情包：自造名]\n后"))
    sent = gateway.tdlib.requests
    kinds = [r["input_message_content"]["@type"] for r in sent]
    assert kinds == ["inputMessageText"]
    joined = "".join(r["input_message_content"]["text"]["text"] for r in sent)
    assert "[表情包" not in joined


def test_send_reply_plain_text_single_message(gateway):
    asyncio.run(gateway._send_reply(42, "就一句话"))
    assert len(gateway.tdlib.requests) == 1
    assert gateway.tdlib.requests[0]["input_message_content"]["text"]["text"] == "就一句话"
