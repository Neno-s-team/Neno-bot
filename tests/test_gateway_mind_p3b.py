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


def test_uncertain_no_ack_no_resend__old(tmp_path, monkeypatch):
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


def test_mark_read_mapping_and_fallback__old(tmp_path, monkeypatch):
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


def test_flood_wait_pauses_and_resends_after_pause__old(tmp_path, monkeypatch):
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


def test_retract_three_states__old(tmp_path, monkeypatch):
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





# ───────────────────────── P3b 修正轮：状态机 / 核对 / 撤回重试 / 兜底 ─────────────────────────


class FloodOnceTdlib(FakeTdlib):
    """第一次 sendMessage 抛 429，之后成功；模拟暂停结束后重发。"""

    def __init__(self):
        super().__init__()
        self.flooded_once = False

    async def request(self, payload, timeout=None):
        if payload.get("@type") == "sendMessage":
            if not self.flooded_once:
                self.flooded_once = True
                from telegram_gateway.tdlib_client import TdlibError
                raise TdlibError({"@type": "error", "code": 429,
                                  "message": "Too Many Requests: retry after 5"})
            self.requests.append(payload)
            local_id = len(self.requests)
            return {"@type": "message", "id": local_id}
        self.requests.append(payload)
        return await super().request(payload, timeout)


class OldUncertainTdlib(FakeTdlib):
    """先真实发送一次并进入 uncertain，之后不再响应 sendMessage。"""

    def __init__(self):
        super().__init__(uncertain_ids={1})

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        if payload.get("@type") == "getMessage":
            return {"@type": "message", "id": 903, "is_outgoing": True,
                    "content": {"@type": "messageText"}}
        if payload.get("@type") == "sendMessage":
            return {"@type": "message", "id": 1}
        return await super().request(payload, timeout)



def test_flood_wait_then_resent_and_acked(tmp_path, monkeypatch):
    """429 → 本地退回 pending，暂停结束后同一 delivery 被真正发出并 ack sent。"""
    tdlib = FloodOnceTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    item = _text_item(77, "暂停后重发")
    from telegram_gateway.mind_runner import ChatFloodWait
    try:
        asyncio.run(runner._execute(42, item))
    except ChatFloodWait:
        pass
    local = runner.outbox.get(77)
    assert local["state"] == "pending" and local["acked"] == 0
    assert local["attempts"] == 1
    runner._pause_until.clear()
    asyncio.run(runner._execute(42, item))
    local = runner.outbox.get(77)
    sends = [r for r in tdlib.requests if r.get("@type") == "sendMessage"]
    assert len(sends) == 1, "429 那次未交给 TDLib，不计入 sendMessage"
    assert local["state"] == "sent" and local["acked"] == 1
    assert core.acks and core.acks[0]["status"] == "sent"


def test_submitting_resume_goes_to_reconcile_not_resend(tmp_path, monkeypatch):
    """状态 submitting（崩在发送途中）→ 重启后不重发，走 getMessage 核对。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    runner.outbox.begin(88, "tg:42", "text", payload_json='{"text": "崩了"}')
    runner.outbox.mark_submitting(88)
    runner.outbox.set_local_id(88, 501)
    item = _text_item(88, "崩了")
    asyncio.run(runner._execute(42, item))
    sends = [r for r in tdlib.requests if r.get("@type") == "sendMessage"]
    assert sends == [], "submitting 恢复不重发"
    gets = [r for r in tdlib.requests if r.get("@type") == "getMessage"]
    assert gets, "进入核对"


class ReconcileTdlib(FakeTdlib):
    def __init__(self, message_result, uncertain_ids=()):
        super().__init__(uncertain_ids=list(uncertain_ids))
        self.message_result = message_result
        self.get_calls = 0

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        if payload.get("@type") == "getMessage":
            self.get_calls += 1
            return self.message_result
        return await super().request(payload, timeout)


def _uncertain_row(runner, delivery_id, local_id, kind="text"):
    runner.outbox.begin(delivery_id, "tg:42", kind, payload_json="{}")
    runner.outbox.mark_uncertain(delivery_id, local_id=local_id)


def test_reconcile_late_receipt_sent_and_acked(tmp_path, monkeypatch):
    """超时后回执到达：先 getMessage 核对已发出 → sent 并 ack。"""
    tdlib = ReconcileTdlib({"@type": "message", "id": 900, "is_outgoing": True,
                            "content": {"@type": "messageText", "text": {"text": "晚到"}}})
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    _uncertain_row(runner, 91, 601)
    asyncio.run(runner._execute(42, _text_item(91, "晚到")))
    local = runner.outbox.get(91)
    assert local["state"] == "sent" and local["acked"] == 1
    assert core.acks and core.acks[0]["status"] == "sent"
    # 回执晚到：observer 按 local_id 匹配，仍 sent（幂等）
    runner.receipt({"@type": "updateMessageSendSucceeded", "old_message_id": 601,
                    "message": {"id": 900, "chat_id": 42}})


def test_reconcile_get_message_confirms_sent(tmp_path, monkeypatch):
    tdlib = ReconcileTdlib({"@type": "message", "id": 901, "is_outgoing": True,
                            "content": {"@type": "messageText", "text": {"text": "确认"}}})
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    _uncertain_row(runner, 92, 602)
    asyncio.run(runner._execute(42, _text_item(92, "确认")))
    local = runner.outbox.get(92)
    assert local["state"] == "sent" and local["final_id"] == 901 and local["acked"] == 1


def test_reconcile_get_message_failed(tmp_path, monkeypatch):
    tdlib = ReconcileTdlib({"@type": "message", "id": 902, "is_outgoing": True,
                            "sending_state": {"@type": "messageSendingStateFailed"},
                            "content": {"@type": "messageText"}})
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    _uncertain_row(runner, 93, 603)
    asyncio.run(runner._execute(42, _text_item(93, "失败确认")))
    local = runner.outbox.get(93)
    assert local["state"] == "failed" and local["acked"] == 1
    assert core.acks and core.acks[0]["status"] == "failed"


def test_uncertain_timeout_needs_attention_no_resend(tmp_path, monkeypatch):
    """超过 NENO_MIND_UNCERTAIN_MAX_SEC → needs_attention；sendMessage 只被调用过 1 次。"""
    tdlib = OldUncertainTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    asyncio.run(runner._execute(42, _text_item(94, "超时")))
    local = runner.outbox.get(94)
    assert local["state"] == "uncertain" and local["acked"] == 0
    # 把 updated_at 拨到 1000 秒前，越过 NENO_MIND_UNCERTAIN_MAX_SEC
    runner.outbox.store.write(
        "UPDATE mind_outbox SET updated_at=? WHERE delivery_id=?",
        (time.time() - 1000.0, 94),
    )
    asyncio.run(runner._execute(42, _text_item(94, "超时")))
    local = runner.outbox.get(94)
    assert local["state"] == "needs_attention" and local["acked"] == 0
    sends = [r for r in tdlib.requests if r.get("@type") == "sendMessage"]
    assert len(sends) == 1, "真实 sendMessage 只发生过一次"
    # 再下发也保持 needs_attention
    asyncio.run(runner._execute(42, _text_item(94, "超时")))
    local = runner.outbox.get(94)
    assert local["state"] == "needs_attention"


class RetractFlakyCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    async def mind_retract(self, trace_id):
        self.retracts.append(trace_id)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("retract network down")
        return {"ok": True, "trace_id": trace_id}


def test_retract_retry_persists_and_succeeds_next_sweep(tmp_path, monkeypatch):
    """retract 第一次失败 → 下一轮成功 → retract_state='done'。"""
    gw, core = _make_gateway(tmp_path, monkeypatch, core=RetractFlakyCore())
    store = gw.delivery.store
    now = time.time()
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,trace_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("tg:42:7", 42, 7, "{}", "tr-7", "done", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [7],
         "is_permanent": True, "from_cache": False}, 42))
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:7'")
    assert row["retract_state"] == "retry", "失败持久化为待重试"
    # MindRunner 每 10 秒扫一次；直接调一次 sweep
    asyncio.run(gw._mind_runner._retry_retracts_once())
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:7'")
    assert row["retract_state"] == "done"


def test_retract_retryable_state_marks_pending_not_cancel(tmp_path, monkeypatch):
    """payload 已生成（retryable）但无 trace：撤回 → pending，prepare 完成后补撤，不直接取消。"""
    gw, core = _make_gateway(tmp_path, monkeypatch)
    store = gw.delivery.store
    now = time.time()
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("tg:42:8", 42, 8, "{}", '{"session_id":"tg:42"}', "retryable", now, now))
    asyncio.run(gw._handle_retract_update(
        {"@type": "updateDeleteMessages", "chat_id": 42, "message_ids": [8],
         "is_permanent": True, "from_cache": False}, 42))
    row = store.one("SELECT * FROM inbox WHERE key='tg:42:8'")
    assert row["retract_state"] == "pending" and row["state"] == "retryable"
    assert core.retracts == [], "没 trace 不调 core"
    asyncio.run(gw.delivery._retract_if_pending("tg:42:8", "tr-8"))
    assert "tr-8" in core.retracts


def test_mark_read_fallback_anchored_to_send_at(tmp_path, monkeypatch):
    """看手机之后又来新消息：兜底不会把新消息标成已读。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    store = runner.outbox.store
    send_at = time.time()
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("tg:42:50", 42, 550, "{}", send_at - 100, send_at))
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("tg:42:51", 42, 551, "{}", send_at + 100, send_at + 100))
    asyncio.run(runner._execute(42, {
        "id": 95, "session_id": "tg:42", "kind": "mark_read", "send_at": send_at,
        "payload": {"trace_ids": ["tr-missing"]},
    }))
    view = next(r for r in tdlib.requests if r.get("@type") == "viewMessages")
    assert view["message_ids"] == [550], "兜底只取 created_at<=send_at 的最新一条"


async def _run_chat_loops(runner, tdlib, chat_items, *, run_seconds=0.5):
    concurrency = max(1, int(getattr(runner.gateway.config, "mind_max_concurrency", 4)))
    semaphore = asyncio.Semaphore(concurrency)
    loops = [asyncio.create_task(runner._chat_loop(chat, semaphore)) for chat in chat_items]
    await asyncio.sleep(run_seconds)
    runner.stopping = True
    for loop in loops:
        loop.cancel()
    for loop in loops:
        try:
            await loop
        except asyncio.CancelledError:
            pass


def test_per_chat_loops_do_not_block_each_other(tmp_path, monkeypatch):
    """会话 A 长 typing 时，会话 B 的 text 能按时发出（独立循环 + Semaphore）。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib, poll=0.05)
    runner = gw._mind_runner
    core.due_items = {
        42: [{"id": 200, "session_id": "tg:42", "kind": "typing",
              "payload": {"duration": 3.0}, "send_at": time.time()}],
        43: [{"id": 201, "session_id": "tg:43", "kind": "text",
              "payload": {"text": "B 的消息"}, "send_at": time.time()}],
    }
    original_due = core.mind_due
    async def due_side(*, session_id, limit=50):
        return core.due_items[int(session_id.split(":")[1])]
    core.due_items_map = core.due_items
    async def patched_due(*, session_id, limit=50):
        return core.due_items_map[int(session_id.split(":")[1])]
    core.mind_due = patched_due
    asyncio.run(_run_chat_loops(runner, tdlib, [42, 43], run_seconds=0.4))
    sent_b = [r for r in tdlib.requests
              if r.get("@type") == "sendMessage" and r.get("chat_id") == 43]
    assert sent_b, "A 在 typing 时 B 的 text 已发出"


def test_send_receipt_updates_mind_outbox_and_acks(tmp_path, monkeypatch):
    """observer 回执按 local_id 匹配 mind_outbox：sent → ack sent；429 失败回执 → pending + 暂停。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    _uncertain_row(runner, 96, 700)

    async def scenario():
        runner.receipt({"@type": "updateMessageSendSucceeded", "old_message_id": 700,
                        "message": {"id": 960, "chat_id": 42}})
        await asyncio.sleep(0)
    asyncio.run(scenario())
    local = runner.outbox.get(96)
    assert local["state"] == "sent" and local["final_id"] == 960
    assert core.acks and core.acks[0]["status"] == "sent"

    _uncertain_row(runner, 97, 701)

    async def flood_scenario():
        runner.receipt({"@type": "updateMessageSendFailed", "old_message_id": 701,
                        "message": {"chat_id": 42},
                        "error": {"code": 429, "message": "Too Many Requests: retry after 30"}})
        await asyncio.sleep(0)
    asyncio.run(flood_scenario())
    local = runner.outbox.get(97)
    assert local["state"] == "pending", "429 失败回执退回 pending"
    assert runner._pause_until.get(42, 0.0) > time.time(), "会话被暂停"
    assert all(a["id"] != 97 for a in core.acks), "429 不 ack"



# ───────────────────── P3 收尾：inflight / 429 等待结果 / 历史匹配 ─────────────────────


class FloodedSendResultTdlib(FakeTdlib):
    """sendMessage 成功，但等待发送结果返回 429 失败；第二次重发成功。"""

    def __init__(self):
        super().__init__()
        self.result_calls = 0

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        if payload.get("@type") == "sendMessage":
            local_id = len(self.requests)
            return {"@type": "message", "id": local_id}
        return {"@type": "ok"}

    async def wait_message_send_result(self, message_id, timeout=None):
        self.result_calls += 1
        if self.result_calls == 1:
            return {"@type": "updateMessageSendFailed", "old_message_id": message_id,
                    "error": {"code": 429, "message": "Too Many Requests: retry after 5"}}
        return {"@type": "updateMessageSendSucceeded", "old_message_id": message_id,
                "message": {"id": message_id, "chat_id": 42}}


def test_inflight_429_waits_then_pending_and_resend(tmp_path, monkeypatch):
    """等待结果期间收到 429 发送失败 → pending、不 ack、暂停；重发后 sent。"""
    tdlib = FloodedSendResultTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    from telegram_gateway.mind_runner import ChatFloodWait
    item = _text_item(300, "限流重试")
    try:
        asyncio.run(runner._execute(42, item))
    except ChatFloodWait:
        pass
    local = runner.outbox.get(300)
    assert local["state"] == "pending" and local["acked"] == 0
    assert runner._pause_until.get(42, 0.0) > time.time(), "会话暂停"
    assert all(a["id"] != 300 for a in core.acks), "429 不 ack"
    # observer 在等待期间也不插手（inflight 已移除，但该回执由等待方处理过了）
    runner._pause_until.clear()
    asyncio.run(runner._execute(42, item))
    local = runner.outbox.get(300)
    assert local["state"] == "sent" and local["acked"] == 1
    sent_acks = [a for a in core.acks if a["id"] == 300 and a["status"] == "sent"]
    assert len(sent_acks) == 1, "成功只收到 1 次 ack"


class HistoryTdlib(FakeTdlib):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages
        self.getMessage_raises = True

    async def request(self, payload, timeout=None):
        self.requests.append(payload)
        if payload.get("@type") == "getMessage" and self.getMessage_raises:
            from telegram_gateway.tdlib_client import TdlibError
            raise TdlibError({"@type": "error", "code": 400, "message": "message not found"})
        if payload.get("@type") == "getChatHistory":
            return {"@type": "messages", "messages": self.messages}
        return await super().request(payload, timeout)


def test_reconcile_history_match_text(tmp_path, monkeypatch):
    """local_id 失效、历史里能找到同文本消息 → sent 并 ack。"""
    messages = [
        {"@type": "message", "id": 800, "is_outgoing": True, "date": int(time.time()),
         "content": {"@type": "messageText", "text": {"text": "历史里找到我"}}},
    ]
    tdlib = HistoryTdlib(messages)
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    runner.outbox.begin(301, "tg:42", "text", payload_json='{"text": "历史里找到我"}')
    runner.outbox.mark_submitting(301)
    runner.outbox.mark_uncertain(301, local_id=999)
    asyncio.run(runner._execute(42, _text_item(301, "历史里找到我")))
    local = runner.outbox.get(301)
    assert local["state"] == "sent" and local["final_id"] == 800 and local["acked"] == 1


def test_reconcile_history_skips_taken_final_id(tmp_path, monkeypatch):
    """历史里同文本消息已被其他行 final_id 占用 → 不匹配，维持 uncertain。"""
    messages = [
        {"@type": "message", "id": 801, "is_outgoing": True, "date": int(time.time()),
         "content": {"@type": "messageText", "text": {"text": "被占用"}}},
    ]
    tdlib = HistoryTdlib(messages)
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    # 另一行已把 801 认走
    runner.outbox.begin(302, "tg:42", "text", payload_json='{"text": "被占用"}')
    runner.outbox.mark_submitting(302)
    runner.outbox.mark_uncertain(302, local_id=998)
    runner.outbox.mark_sent(302, final_id=801)
    # 目标行进入核对
    runner.outbox.begin(303, "tg:42", "text", payload_json='{"text": "被占用"}')
    runner.outbox.mark_submitting(303)
    runner.outbox.mark_uncertain(303, local_id=997)
    asyncio.run(runner._execute(42, _text_item(303, "被占用")))
    local = runner.outbox.get(303)
    assert local["state"] == "uncertain", "同文本消息被占用，不匹配"
    assert core.acks == [], "不 ack"


def test_receipt_skips_inflight(tmp_path, monkeypatch):
    """inflight 期间 observer 回执不插手（避免双写/抢先 ack）。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    runner._inflight.add(310)
    runner.receipt({"@type": "updateMessageSendSucceeded", "old_message_id": 650,
                    "message": {"id": 950, "chat_id": 42}})
    assert core.acks == []


def test_stale_outbox_row_from_other_session_does_not_block(tmp_path, monkeypatch):
    """outbox 里同 id 的残留行（别的会话/类型）不能吞掉新投递。"""
    tdlib = FakeTdlib()
    gw, core = _make_gateway(tmp_path, monkeypatch, tdlib=tdlib)
    runner = gw._mind_runner
    runner.outbox.begin(30, "tg:43", "sticker")
    runner.outbox.mark_failed(30, "unknown sticker")
    runner.outbox.mark_ack(30)
    asyncio.run(runner._execute(42, _text_item(30, "要发出去")))
    assert len(tdlib.requests) == 1
    local = runner.outbox.get(30)
    assert local["session_id"] == "tg:42" and local["kind"] == "text"
    assert local["state"] == "sent" and local["acked"] == 1
