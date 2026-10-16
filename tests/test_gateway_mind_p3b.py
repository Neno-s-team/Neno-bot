"""P3b 网关异步投递测试。假 TDLib + 假 core，无真实调用。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import telegram_gateway.runtime as runtime
from telegram_gateway.mind_outbox import MindOutbox


class FakeTdlib:
    def __init__(self, *, fail_codes=None, uncertain_ids=None, flood_ids=None):
        self.requests = []
        self.fail_codes = fail_codes or {}
        self.uncertain_ids = set(uncertain_ids or ())
        self.flood_ids = set(flood_ids or ())
        self._seen_status = set()

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        rid = payload.get("@type")
        if rid == "sendMessage":
            local_id = len(self.requests)
            if local_id in self.flood_ids:
                from telegram_gateway.tdlib_client import TdlibError
                raise TdlibError({"@type": "error", "code": 429,
                                  "message": "Too Many Requests: retry after 30"})
            if local_id in self.fail_codes:
                from telegram_gateway.tdlib_client import TdlibError
                raise TdlibError({"@type": "error", "code": self.fail_codes[local_id], "message": "boom"})
            return {"@type": "message", "id": local_id}
        return {"@type": "ok"}

    async def wait_message_send_result(self, message_id, timeout=None):
        if message_id in self.uncertain_ids:
            raise asyncio.TimeoutError()
        return {"@type": "updateMessageSendSucceeded", "old_message_id": message_id}


class FakeCore:
    def __init__(self, *args, **kwargs):
        self.due_items = []
        self.acks = []
        self.retracts = []
        self.ack_fail_ids = set()
        self.due_calls = 0

    async def close(self):
        pass

    async def chat(self, **kwargs):
        raise AssertionError("sync chat should not be called in async tests")

    async def mind_due(self, *, session_id, limit=50):
        self.due_calls += 1
        return self.due_items

    async def mind_ack(self, delivery_id, status, error=None):
        if delivery_id in self.ack_fail_ids:
            self.ack_fail_ids.discard(delivery_id)
            raise RuntimeError("ack network glitch")
        self.acks.append({"id": delivery_id, "status": status, "error": error})
        return {"ok": True, "id": delivery_id, "status": status}

    async def mind_retract(self, trace_id):
        self.retracts.append(trace_id)
        return {"ok": True, "trace_id": trace_id}


def _sticker_dir(tmp_path):
    (tmp_path / "a.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 22)
    (tmp_path / "custom_stickers.json").write_text(
        '[{"description": "喵", "fileName": "a.png"}]', encoding="utf-8"
    )
    return tmp_path


def _make_gateway(tmp_path, monkeypatch, *, mind_async=True, tdlib=None, core=None,
                  presence=False, poll=0.05, concurrency=4):
    # 每个测试独立 operations.db
    import app.storage.operations as operations_storage

    operations_storage._instance = None
    monkeypatch.setenv("NENO_OPERATIONS_DB", str(tmp_path / "ops.db"))
    monkeypatch.setattr(runtime, "TdlibClient", lambda *a, **k: tdlib or FakeTdlib())
    core_instance = core or FakeCore()
    monkeypatch.setattr(runtime, "NenoCoreClient",
                        lambda *a, **k: core_instance)
    config = SimpleNamespace(
        sidecar_binary=tmp_path / "sidecar.exe",
        tdjson_library=tmp_path / "tdlib.dll",
        core_url="http://127.0.0.1:9",
        internal_token="",
        sticker_pack_dir=_sticker_dir(tmp_path),
        allowed_chat_ids=frozenset({42, 43}),
        mind_async=mind_async,
        mind_poll_seconds=poll,
        mind_max_concurrency=concurrency,
        mind_presence=presence,
    )
    gw = runtime.TelegramGateway(config)
    return gw, core_instance


def _text_item(delivery_id, text="hello", chat=42):
    return {"id": delivery_id, "session_id": f"tg:{chat}", "cycle_id": "c1",
            "seq": 0, "kind": "text", "payload": {"text": text},
            "send_at": time.time(), "status": "claimed"}


def test_async_prepare_accepted_marks_done(tmp_path, monkeypatch):
    gw, core = _make_gateway(tmp_path, monkeypatch)
    core.due_items = []
    # delivery.prepare 走 accepted 分支
    async def accepted_chat(**kwargs):
        return {"status": "accepted", "trace_id": "tr-1"}

    monkeypatch.setattr(gw.core, "chat", accepted_chat)
    key = "tg:42:100"
    gw.delivery.store.write(
        "INSERT INTO inbox(key,chat_id,message_id,data,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (key, 42, 100, json.dumps({"chat_id": 42, "message_id": 100, "text": "hi",
                                   "content_type": "text", "caption": "hi"}),
         "received", time.time(), time.time()),
    )
    row = gw.delivery.store.one("SELECT * FROM inbox WHERE key=?", (key,))
    asyncio.run(gw.delivery.prepare(row))
    row = gw.delivery.store.one("SELECT * FROM inbox WHERE key=?", (key,))
    assert row["state"] == "done" and row["trace_id"] == "tr-1"
    assert gw.delivery.store.one("SELECT 1 FROM outbox WHERE inbox_key=?", (key,)) is None


def test_sync_mode_unchanged(tmp_path, monkeypatch):
    """同步模式：gateway 不建 MindRunner，prepare 仍走 ready/outbox 路径。"""
    gw, core = _make_gateway(tmp_path, monkeypatch, mind_async=False)
    assert gw._mind_runner is None
    async def sync_chat(**kwargs):
        return {"reply": "同步回复", "trace_id": "tr-2"}

    monkeypatch.setattr(gw.core, "chat", sync_chat)
    key = "tg:42:101"
    gw.delivery.store.write(
        "INSERT INTO inbox(key,chat_id,message_id,data,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (key, 42, 101, json.dumps({"chat_id": 42, "message_id": 101, "text": "hi",
                                   "content_type": "text", "caption": "hi"}),
         "received", time.time(), time.time()),
    )
    row = gw.delivery.store.one("SELECT * FROM inbox WHERE key=?", (key,))
    asyncio.run(gw.delivery.prepare(row))
    row = gw.delivery.store.one("SELECT * FROM inbox WHERE key=?", (key,))
    assert row["state"] == "ready" and row["reply"] == "同步回复"


def test_same_delivery_not_resent(tmp_path, monkeypatch):
    """同一 delivery_id 第二次下发不会重复 sendMessage。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    item = _text_item(1, "只发一次")
    asyncio.run(runner._execute(42, item))
    sent_count = len(tdlib.requests)
    assert sent_count == 1
    # core 租约过期重新下发同一项
    asyncio.run(runner._execute(42, item))
    assert len(tdlib.requests) == sent_count, "不重复 sendMessage"
    local = runner.outbox.get(1)
    assert local["acked"] == 1 and local["state"] == "sent"


def test_uncertain_no_ack_no_resend(tmp_path, monkeypatch):
    """结果不确定时：不 ack、也不重发。"""
    tdlib = FakeTdlib(uncertain_ids={1})
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    item = _text_item(1, "结果不明")
    asyncio.run(runner._execute(42, item))
    local = runner.outbox.get(1)
    assert local["state"] == "uncertain" and local["acked"] == 0
    assert core.acks == []
    # 第二次下发仍不重发
    asyncio.run(runner._execute(42, item))
    sends = [r for r in tdlib.requests if r.get("@type") == "sendMessage"]
    assert len(sends) == 1


def test_mark_read_mapping_and_fallback(tmp_path, monkeypatch):
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    # inbox 有 t1 映射
    runner.outbox.store.write(
        "INSERT INTO inbox(key,chat_id,message_id,data,trace_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("tg:42:1", 42, 111, "{}", "tr-a", "done", time.time(), time.time()),
    )
    asyncio.run(runner._execute(42, {
        "id": 10, "session_id": "tg:42", "kind": "mark_read",
        "payload": {"trace_ids": ["tr-a", "tr-missing"]},
    }))
    view = next(r for r in tdlib.requests if r.get("@type") == "viewMessages")
    assert 111 in view["message_ids"]
    assert len(view["message_ids"]) == 2, "missing trace 走兜底（取最新一条）"
    local = runner.outbox.get(10)
    assert local["state"] == "sent"


def test_typing_repeats_every_4s(tmp_path, monkeypatch):
    """typing 按 4 秒间隔重复，总时长符合 duration。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    # 假时钟：monotonic 推进，sleep 快进
    sleeps = []
    import telegram_gateway.mind_runner as mr
    fake_clock = {"t": 0.0}
    real_monotonic = time.monotonic
    monkeypatch.setattr(mr.time, "monotonic", lambda: fake_clock["t"])

    async def fast_sleep(delay, *a, **kw):
        if delay:
            sleeps.append(delay)
            fake_clock["t"] += delay
        return None

    monkeypatch.setattr(mr.asyncio, "sleep", fast_sleep)
    asyncio.run(runner._execute(42, {
        "id": 20, "session_id": "tg:42", "kind": "typing",
        "payload": {"duration": 9.0}, "cycle_id": "c", "seq": 0,
    }))
    actions = [r for r in tdlib.requests if r.get("@type") == "sendChatAction"]
    assert 2 <= len(actions) <= 3, "9 秒 / 4 秒间隔 ≈ 3 次"
    assert sum(sleeps) >= 9.0 - 0.5, "总时长覆盖 duration"
    local = runner.outbox.get(20)
    assert local["state"] == "sent" and local["acked"] == 1


def test_unknown_sticker_acks_failed(tmp_path, monkeypatch):
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    asyncio.run(runner._execute(42, {
        "id": 30, "session_id": "tg:42", "kind": "sticker",
        "payload": {"name": "不存在的表情"},
    }))
    assert core.acks and core.acks[0]["status"] == "failed"
    assert core.acks[0]["error"] == "unknown sticker"
    local = runner.outbox.get(30)
    assert local["state"] == "failed"


def test_flood_wait_pauses_and_continues(tmp_path, monkeypatch):
    """429 → 不 ack，暂停后继续（本地保持，下轮重领后 resume）。"""
    tdlib = FakeTdlib(flood_ids={1})
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    item = _text_item(1, "会被限流")

    async def run_one():
        try:
            await runner._execute(42, item)
        except Exception as exc:
            from telegram_gateway.mind_runner import ChatFloodWait
            assert isinstance(exc, ChatFloodWait)
            runner._pause_until[42] = time.time() + exc.retry_after

    asyncio.run(run_one())
    assert core.acks == [], "429 不 ack"
    local = runner.outbox.get(1)
    assert local is not None, "本地记录保持"
    # 暂停结束后：core 重发同一项 → resume 路径只推进
    assert runner._pause_until[42] > time.time()


def test_retract_three_states(tmp_path, monkeypatch):
    gw, core = _make_gateway(tmp_path, monkeypatch)
    store = gw.delivery.store
    now = time.time()
    # ① 还没交给 core（received、无 trace）→ cancelled，不调 core
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                ("tg:42:1", 42, 1, "{}", "received", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [1],
         "is_permanent": True, "from_cache": False}, 42))
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:1'")
    assert row["state"] == "cancelled" and row["retract_state"] == "cancelled"
    assert core.retracts == []

    # ② 已有 trace → 调 retract
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,trace_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("tg:42:2", 42, 2, "{}", "tr-2", "done", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [2],
         "is_permanent": True, "from_cache": False}, 42))
    assert "tr-2" in core.retracts

    # ③ generating → pending，等 trace 后补撤
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                ("tg:42:3", 42, 3, "{}", "generating", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [3],
         "is_permanent": True, "from_cache": False}, 42))
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:3'")
    assert row["retract_state"] == "pending"
    # prepare 返回 accepted 后补撤
    asyncio.run(gw.delivery._retract_if_pending("tg:42:3", "tr-3"))
    assert "tr-3" in core.retracts


def test_retract_ignores_from_cache(tmp_path, monkeypatch):
    gw, core = _make_gateway(tmp_path, monkeypatch)
    store = gw.delivery.store
    now = time.time()
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,trace_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("tg:42:9", 42, 9, "{}", "tr-9", "done", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [9],
         "is_permanent": True, "from_cache": True}, 42))
    assert core.retracts == []
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:9'")
    assert row["state"] == "done"


def test_ack_retry_on_failure(tmp_path, monkeypatch):
    """ack 调用失败后：本地 acked=0，重试后成功。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    core.ack_fail_ids = {1}
    runner = gw._mind_runner
    asyncio.run(runner._execute(42, _text_item(1, "ack 会失败一次")))
    local = runner.outbox.get(1)
    assert local["state"] == "sent" and local["acked"] == 0, "发送成功但 ack 失败"
    # core 重新下发（模拟租约过期）→ resume 补 ack
    asyncio.run(runner._execute(42, _text_item(1, "ack 会失败一次")))
    local = runner.outbox.get(1)
    assert local["acked"] == 1


def test_chat_serial_and_parallel(tmp_path, monkeypatch):
    """同一会话串行：_drain_chat 持锁；不同会话各自持锁不互斥。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    lock42 = runner._lock(42)
    lock43 = runner._lock(43)
    assert lock42 is not lock43
    # 42 在锁内，43 仍可进入
    async def scenario():
        async with lock42:
            acquired = []

            async def try_43():
                try:
                    async with asyncio.timeout(0.2):
                        async with lock43:
                            acquired.append(43)
                except asyncio.TimeoutError:
                    pass
            async def try_42():
                try:
                    async with asyncio.timeout(0.1):
                        async with lock42:
                            acquired.append(42)
                except asyncio.TimeoutError:
                    pass
            await asyncio.gather(try_43(), try_42())
            return acquired
    acquired = asyncio.run(scenario())
    assert 43 in acquired and 42 not in acquired


def test_presence_toggle(tmp_path, monkeypatch):
    """presence：启动 offline，动作前 online，60 秒后 offline。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib, presence=True)
    runner = gw._mind_runner
    import telegram_gateway.mind_runner as mr
    fake_clock = {"t": 0.0}
    monkeypatch.setattr(mr.time, "time", lambda: fake_clock["t"])

    async def fast_sleep(delay, *a, **kw):
        if delay:
            fake_clock["t"] += delay
        return None

    monkeypatch.setattr(mr.asyncio, "sleep", fast_sleep)
    asyncio.run(runner._ensure_offline_at_start())
    options = [r for r in tdlib.requests if r.get("@type") == "setOption"]
    assert options and options[-1]["value"]["value"] is False
    asyncio.run(runner._presence_online())
    options = [r for r in tdlib.requests if r.get("@type") == "setOption"]
    assert options[-1]["value"]["value"] is True, "动作时上线"
    runner._presence_until = fake_clock["t"] - 1
    asyncio.run(runner._presence_worker())
    options = [r for r in tdlib.requests if r.get("@type") == "setOption"]
    assert options[-1]["value"]["value"] is False, "最后一个动作后下线"
