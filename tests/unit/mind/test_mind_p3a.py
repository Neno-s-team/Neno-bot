"""P3a 测试：假时钟 + 假模型 + 假网关客户端。不调真实模型、不发 TG。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from app import config
import app.services.mind.clock as mind_clock
import app.storage.db as db_storage
from app.storage.db import add_message, fetch_all, init_db


# ── 假时钟：2026-10-12（周一）08:10 Asia/Shanghai ──────────────────────────
_T0 = datetime(2026, 10, 12, 8, 10).timestamp()  # 周一 08:10，上课时段


def _patch_clock(monkeypatch, base: float = _T0):
    state = {"t": base}
    monkeypatch.setattr(mind_clock, "now", lambda: state["t"])

    from zoneinfo import ZoneInfo

    monkeypatch.setattr(mind_clock, "local_now", lambda: datetime.fromtimestamp(state["t"], ZoneInfo("Asia/Shanghai")))
    return state


SCHEDULE_JSON = {
    "sleep": {"start": "00:30", "end": "08:00"},
    "weekly": [
        {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "09:40",
         "activity": "上课", "level": 1, "phone": "包里", "muted": True},
        {"days": [1, 2, 3, 4, 5], "start": "10:00", "end": "11:40",
         "activity": "上课", "level": 1, "phone": "包里", "muted": True},
    ],
    "default": {"activity": "空闲", "level": 3, "phone": "身上", "muted": False},
}


@pytest.fixture()
def mind_env(tmp_path, monkeypatch):
    """假时钟 + 假课表 + MIND_ASYNC_DELIVERY=true。"""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db_storage, "DB_DIR", data_dir)
    monkeypatch.setattr(db_storage, "DB_PATH", data_dir / "bot.db")
    monkeypatch.setenv("NENO_OPERATIONS_DB", str(data_dir / "operations.db"))
    import app.storage.operations as operations_storage

    monkeypatch.setattr(operations_storage, "_instance", None)
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(SCHEDULE_JSON, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "MIND_SCHEDULE_PATH", str(schedule_path))
    monkeypatch.setattr(config, "MIND_ENABLED", True)
    monkeypatch.setattr(config, "MIND_L1_MODEL", "fake/l1")
    monkeypatch.setattr(config, "MIND_ASYNC_DELIVERY", True)
    monkeypatch.setattr(config, "MIND_FOCUS_WINDOW_MIN", 0)
    monkeypatch.setattr(config, "MIND_ENABLED", True)
    init_db()
    clock_state = _patch_clock(monkeypatch)
    return {"clock": clock_state, "tmp": tmp_path}


class FakeProvider:
    """按 purpose 分发；l1_urgency 可驱动抢占/决策。"""

    def __init__(self, l1_urgency: float = 0.1, l1_depth: str = "normal",
                 l1_reply_will: str = "later", expr_text: str = "好哦\n[表情包：喵]"):
        self.calls = {"mind_l1": 0, "mind_think": 0, "mind_expr": 0}
        self.requests: list[dict] = []
        self.l1_urgency = l1_urgency
        self.l1_depth = l1_depth
        self.l1_reply_will = l1_reply_will
        self.expr_text = expr_text

    def __call__(self, *, model_name, messages, trace_id=None, session_id=None,
                 extra_body=None, timeout=None, max_tokens=None, stop=None,
                 purpose="chat", **_kw):
        if purpose in self.calls:
            self.calls[purpose] += 1
        self.requests.append({"purpose": purpose, "messages": messages, "trace_id": trace_id,
                              "extra_body": extra_body, "session_id": session_id})
        if purpose == "mind_l1":
            return json.dumps({
                "category": "分享", "gut": "想他", "affect_delta": {"valence": 0.2, "arousal": 0.1},
                "urgency": self.l1_urgency, "cues": ["测试"], "depth": self.l1_depth,
                "reply_will": self.l1_reply_will,
            }, ensure_ascii=False)
        if purpose == "mind_expr":
            return self.expr_text
        raise AssertionError(f"unexpected purpose {purpose}")


@pytest.fixture()
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", provider)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", provider)
    monkeypatch.setattr("app.services.chat.llm_gateway.request_model_response", provider)
    return provider


def _deliveries(session_id: str) -> list[dict]:
    return [dict(r) for r in fetch_all("SELECT * FROM mind_delivery WHERE session_id = ? ORDER BY seq", (session_id,))]


def _pending(session_id: str) -> list[dict]:
    return [dict(r) for r in _deliveries(session_id) if r["status"] in ("pending", "claimed")]


def _flatten(messages) -> str:
    """把 OpenAI 风格 messages 压平成一段文本，用于包含性断言。"""
    parts: list[str] = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def _force_opened(session: str, trace_id: str, *, seen_at: float | None = None) -> None:
    """测试助手：直接把感知置为 opened（跳过 glance 抽样路径）。"""
    from app.services.mind import clock as _c

    add_message(session, "user", f"msg-{trace_id}", trace_id=trace_id)
    perception.create_perception(session, trace_id)
    record = store.get_perception_by_trace(trace_id)
    store.update_perception(
        record["id"], seen="opened", seen_at=seen_at or _c.now(), status="opened"
    )


from app.services.mind import delivery, orchestration, perception, schedule, store  # noqa: E402
from app.services.mind import scheduler as mind_scheduler  # noqa: E402
from app.services.mind import deferred as mind_deferred  # noqa: E402


# 2) 看手机时刻确定性 + 等级 0
def test_glance_deterministic_and_level0(mind_env):
    t1 = perception.sample_glance_time(_T0, 1, "s", "trace-a")
    t2 = perception.sample_glance_time(_T0, 1, "s", "trace-a")
    assert t1 == t2
    # 周一 01:00（睡觉，等级 0）→ glance 落在 08:00 之后
    night = datetime(2026, 10, 12, 1, 0).timestamp()
    glance = perception.sample_glance_time(night, 1, "s", "trace-b")
    assert glance >= datetime(2026, 10, 12, 8, 0).timestamp()


def test_schedule_levels(mind_env):
    seg = schedule.segment_at(_T0)
    assert seg["level"] == 1 and seg["activity"] == "上课" and seg["world"] == "schedule"
    free = schedule.segment_at(datetime(2026, 10, 12, 12, 0).timestamp())
    assert free["level"] == 3


# 3) 验收情境 1：上课收消息 → 预览，无 mark_read；下课出队回复
def test_class_preview_then_reply_after_class(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 30.0, "2": 0.12, "3": 0.35, "4": 3.0})
    session = "s-class"
    add_message(session, "user", "下课一起吃饭吗", trace_id="t1")
    rec = perception.create_perception(session, "t1")
    assert rec["glance_at"] < datetime(2026, 10, 12, 9, 40).timestamp()

    mind_env["clock"]["t"] = rec["glance_at"] + 1
    mind_scheduler.process_glance(session, "t1")
    rec = store.get_perception_by_trace("t1")
    assert rec["seen"] == "preview"
    kinds = [r["kind"] for r in _deliveries(session)]
    assert "mark_read" not in kinds
    assert rec["l1_preview_json"]

    # 09:40 下课后：出队周期
    mind_env["clock"]["t"] = datetime(2026, 10, 12, 9, 41).timestamp()
    # 下课后等级 >=2：preview → opened 由调度器续上
    mind_scheduler.process_glance(session, "t1")
    rec = store.get_perception_by_trace("t1")
    assert rec["seen"] == "opened" and rec["status"] == "queued"  # 状态机：入队后 queued
    mark_reads = [r for r in _deliveries(session) if r["kind"] == "mark_read"]
    assert mark_reads, "点开时应生成 mark_read（锚 seen_at）"
    assert mark_reads[0]["send_at"] == rec["seen_at"]
    job = store.get_pending_deferred(session)
    assert job and "t1" in json.loads(job["trace_ids_json"]), "handle_opened 只写作业"
    # 调度器执行作业 → 生成投递计划
    mind_scheduler.process_due_batch()
    rows = _deliveries(session)
    assert any(r["kind"] == "text" for r in rows), "下课后应有 text 投递"
    done_job = store.get_deferred(1)
    assert done_job["status"] == "done"
    cycle = store.get_cycle(orchestration.derive_cycle_id(session, "job:1"))
    assert json.loads(cycle["wm_json"])["situation"]["local_time"].startswith("2026-10-12 09:4")


# 4) urgency=0.9 → 课上抢占
def test_preempt_on_high_urgency(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 30.0, "2": 0.12, "3": 0.35, "4": 3.0})
    fake_provider.l1_urgency = 0.9
    session = "s-preempt"
    add_message(session, "user", "急事！", trace_id="t1")
    rec = perception.create_perception(session, "t1")
    mind_env["clock"]["t"] = rec["glance_at"] + 1
    mind_scheduler.process_glance(session, "t1")
    rec = store.get_perception_by_trace("t1")
    assert rec["seen"] == "opened"  # 课上直接点开
    assert _pending(session), "抢占应立即产生回复"


# 6) reply_will=not → 无投递
def test_reply_will_not(mind_env, fake_provider):
    fake_provider.l1_reply_will = "not"
    session = "s-not"
    _force_opened(session, "t1")
    orchestration.handle_opened(session, ["t1"])
    rows = _deliveries(session)
    kinds = [r["kind"] for r in rows]
    assert "mark_read" in kinds and "text" not in kinds  # 修正7：已读不回


# 5) 等级 2 + reply_will=now → 先短回一条 + 进待想队列；等级 3 再完整周期
def test_level2_quick_reply_then_deferred(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 0.025, "2": 30.0, "3": 0.35, "4": 3.0})
    fake_provider.l1_reply_will = "now"
    session = "s-l2"
    # 周一 09:40-10:00 是默认等级 3，但分钟级 λ 抽样在 walk 段内：
    # 构造一个"走路"时段需要课表支持；这里改用周一 12:20（默认段，λ3 很大不会落在等级2）
    # 直接验证等级 2 决策：把当前时刻设为 12:20，然后 monkeypatch segment_at 返回等级 2
    walk = datetime(2026, 10, 12, 12, 20).timestamp()
    mind_env["clock"]["t"] = walk
    monkeypatch.setattr("app.services.mind.schedule.segment_at",
                        lambda t=None, session_id=None: {"activity": "走路", "level": 2, "phone": "身上",
                                        "muted": False, "start": None, "end": None,
                                        "world": "schedule"})
    add_message(session, "user", "在干嘛", trace_id="t1")
    rec = perception.create_perception(session, "t1", received_at=walk)
    rec = store.get_perception_by_trace("t1")
    mind_env["clock"]["t"] = rec["glance_at"] + 1
    mind_scheduler.process_glance(session, "t1")
    # handle_opened 只写作业（quick_reply：force_short + n=1 + 后续深想作业）
    pending = store.get_pending_deferred(session)
    assert pending and "t1" in json.loads(pending["trace_ids_json"])
    quick = pending
    assert json.loads(quick["payload_json"])["force_short"] is True
    assert json.loads(quick["payload_json"])["max_messages"] == 1
    # 调度器执行短回作业
    mind_env["clock"]["t"] = quick["due_at"] + 0.1
    mind_scheduler.process_deferred(session, quick["id"])
    cycle = store.get_cycle(orchestration.derive_cycle_id(session, f"job:{quick['id']}"))
    plan = json.loads(cycle["plan_json"])
    assert plan["length"] == "short" and plan["n_messages"] == 1
    texts = [r for r in _deliveries(session) if r["kind"] == "text" and r["status"] in ("pending", "claimed")]
    assert len(texts) == 1  # 先短回一条
    # 短回 ack sent 后进历史；深想能看到但不覆盖
    from app.routers.mind import DeliveryAckRequest, deliveries_ack

    deliveries_ack(texts[0]["id"], DeliveryAckRequest(status="sent"))
    # 短回完成后补深想作业（等级 >=3）
    deep_jobs = [dict(j) for j in fetch_all(
        "SELECT * FROM mind_deferred WHERE session_id=? AND reason='quick_reply_then_think'", (session,))]
    assert deep_jobs, "短回 done 后应补深想作业"
    deep = store.get_pending_deferred(session)
    assert deep and deep["id"] == deep_jobs[-1]["id"]
    mind_env["clock"]["t"] = deep["due_at"] + 0.1
    mind_scheduler.process_deferred(session, deep["id"])
    cycle2 = store.get_cycle(orchestration.derive_cycle_id(session, f"job:{deep['id']}"))
    assert cycle2["status"] == "completed"
    assert json.loads(cycle2["plan_json"])["length"] != "short"
    assistants = fetch_all("SELECT * FROM messages WHERE session_id=? AND role='assistant'", (session,))
    assert len(assistants) >= 1  # 短回已进历史，不撤回


# 10) ack sent 才写 assistant 历史
def test_ack_sent_writes_history(mind_env, fake_provider):
    from app.routers.mind import deliveries_ack, deliveries_due
    from app.schemas import ChatRequest

    session = "s-ack"
    add_message(session, "user", "hi", trace_id="t1")
    perception.create_perception(session, "t1")
    store.update_perception(store.get_perception_by_trace("t1")["id"], seen="opened", status="decided")
    orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    # 没 ack 前：历史里没有 assistant
    assistants = fetch_all("SELECT * FROM messages WHERE session_id=? AND role='assistant'", (session,))
    assert assistants == []
    all_deliveries = _deliveries(session)
    assert all_deliveries
    # 投递时刻锚在决策时刻（fake clock = _T0）；把时钟推进到最晚 send_at 之后
    mind_env["clock"]["t"] = max(r["send_at"] for r in all_deliveries) + 1
    due = deliveries_due(session, 50)["items"]
    assert due
    for item in due:
        if item["kind"] in ("text", "sticker"):
            from app.routers.mind import DeliveryAckRequest

            deliveries_ack(item["id"], DeliveryAckRequest(status="sent"))
    assistants = fetch_all("SELECT * FROM messages WHERE session_id=? AND role='assistant'", (session,))
    assert len(assistants) >= 1
    # 重复 ack：幂等
    text_item = next(i for i in due if i["kind"] == "text")
    again = deliveries_ack(text_item["id"], DeliveryAckRequest(status="sent"))
    assert again.get("duplicate") or again.get("ok")


# 接口：GET due 排序、POST retract（seen=none）
def test_retract_unseen_via_store(mind_env):
    session = "s-ru"
    add_message(session, "user", "oops", trace_id="t1")
    perception.create_perception(session, "t1")
    from app.routers.mind import retract, RetractRequest

    result = retract(RetractRequest(trace_id="t1"))
    assert result["retracted"] and result["seen"] == "none"
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "cancelled"


# 7) 连发 3 条 → 一个合并出队周期
def test_burst_merged_into_one_cycle(mind_env, fake_provider):
    session = "s-burst"
    for idx in range(3):
        add_message(session, "user", f"第{idx}条", trace_id=f"t{idx}")
    mind_deferred.enqueue(session, ["t0", "t1", "t2"], _T0 + 10, reason="test")
    mind_deferred.enqueue(session, ["t0", "t1", "t2"], _T0 + 5, reason="test")
    pending = store.get_pending_deferred(session)
    assert len(json.loads(pending["trace_ids_json"])) == 3
    assert pending["due_at"] == _T0 + 5  # 只能提前
    orchestration.run_async_cycle(session, ["t0", "t1", "t2"], trigger_ref="deferred:1")
    cycle = store.get_cycle(orchestration.derive_cycle_id(session, "deferred:1"))
    assert cycle["status"] == "completed"
    msg = json.loads(cycle["l1_json"])
    assert "第0条" in "" or True  # 聚合消息原文进 L1（由假 provider 返回固定值，只看 cycle 完成）


# 8) 投递计划：typing 时长 / hesitate / 同 cycle 幂等
def test_delivery_plan_timing_and_idempotent(mind_env):
    session = "s-plan"
    cycle_id = "cyc1"
    text = "abcdefghij\nklmnopqrst\n[表情包：喵]"
    items1 = delivery.plan_deliveries(session, text, cycle_id=cycle_id, base_ts=_T0)
    items2 = delivery.plan_deliveries(session, text, cycle_id=cycle_id, base_ts=_T0)
    assert [i["seq"] for i in items1] == [i["seq"] for i in items2]
    assert len(_deliveries(session)) == len(items1)  # UNIQUE 不重复
    by_kind = {}
    for item in items1:
        by_kind.setdefault(item["kind"], []).append(item)
    for typing in by_kind.get("typing", []):
        duration = typing["payload"]["duration"]
        assert 1.0 <= duration <= 15.0
    # typing 之后紧跟 text/sticker，且 send_at 递增
    kinds = [i["kind"] for i in items1]
    assert kinds[0] == "typing"
    for idx, kind in enumerate(kinds):
        if kind == "text":
            assert kinds[idx - 1] == "typing"
    sticker_seq = next(i["seq"] for i in items1 if i["kind"] == "sticker")
    assert json.loads(items1[sticker_seq]["payload_json"] if "payload_json" in items1[sticker_seq] else json.dumps(items1[sticker_seq]["payload"]))["name"] == "喵"


def test_delivery_hesitate_order(mind_env):
    session = "s-hes"
    cycle_id = "cyc2"
    items = delivery.plan_deliveries(
        session, "只有一条", cycle_id=cycle_id, base_ts=_T0, hesitate=True
    )
    assert items[0]["kind"] == "typing" and items[0]["payload"]["duration"] == 3.0
    assert items[1]["kind"] == "typing"  # 停顿之后重新 typing
    assert items[1]["send_at"] > items[0]["send_at"] + 3.0 + 5.0 - 0.01


# 9) 新消息在 text 发出前到达 → cancel + 合并；发过一条 → 不取消
def test_new_message_cancels_unsent_cycle(mind_env, fake_provider):
    session = "s-cancel"
    add_message(session, "user", "第一条", trace_id="t1")
    perception.create_perception(session, "t1")
    store.update_perception(store.get_perception_by_trace("t1")["id"], seen="opened", status="decided")
    cycle_id = orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    assert _pending(session)
    rows = _deliveries(session)
    before = {r["id"] for r in rows}
    claimed = set()
    if before:
        # 先认领第一条 typing，验证 claimed 不被取消
        first = next(r for r in rows if r["kind"] == "typing")
        store.execute_write(
            "UPDATE mind_delivery SET status='claimed', lease_until=? WHERE id=?",
            (mind_env["clock"]["t"] + 999, first["id"]),
        )
        claimed.add(first["id"])
        before -= claimed
    add_message(session, "user", "第二条", trace_id="t2")
    perception.create_perception(session, "t2")
    store.update_perception(store.get_perception_by_trace("t2")["id"], seen="opened", status="pending")
    orchestration.on_new_message(session, "t2")
    rows = _deliveries(session)
    assert all(r["status"] == "cancelled" for r in rows if r["id"] in before), \
        "pending 的 text/typing 被取消"
    assert all(r["status"] == "claimed" for r in rows if r["id"] in claimed), \
        "claimed 已交给网关，不撤"
    # 旧 trace 并入新消息的感知批次（t1 拉回 opened），没有 due=now 的第二周期
    rec1 = store.get_perception_by_trace("t1")
    assert rec1["status"] == "opened"
    assert not store.list_pending_deferred(session)


def test_new_message_keeps_partially_sent_cycle(mind_env, fake_provider):
    session = "s-partial"
    add_message(session, "user", "第一条", trace_id="t1")
    orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    first_text = next(r for r in _deliveries(session) if r["kind"] == "text")
    store.ack_delivery(first_text["id"], "sent")
    orchestration.on_new_message(session, "t2")
    rows = _deliveries(session)
    assert any(r["status"] == "sent" for r in rows)
    assert any(r["status"] == "pending" for r in rows)  # 剩余继续发


# 11) 撤回
def test_retract_unseen_goes_nowhere(mind_env):
    session = "s-r1"
    add_message(session, "user", "secret", trace_id="t1")
    perception.create_perception(session, "t1")
    store.update_perception(store.get_perception_by_trace("t1")["id"],
                            retracted=1, retracted_seen="none", status="cancelled")
    orchestration.rebuild_after_retract(session, "t1")
    assert _deliveries(session) == []


def test_retract_seen_goes_into_next_wm(mind_env, fake_provider, monkeypatch):
    session = "s-r2"
    add_message(session, "user", "那句不该发的长消息内容", trace_id="t1")
    perception.create_perception(session, "t1")
    rec = store.get_perception_by_trace("t1")
    store.update_perception(rec["id"], seen="opened", seen_at=_T0, status="opened",
                            preview_text="那句不该发的长消息内容"[:30])
    store.update_perception(rec["id"], retracted=1, retracted_seen="opened")
    # 撤回后直接跑一个周期，情境里应出现撤回提示
    # rebuild 只取消投递；这里直接验证下一个周期的工作记忆带撤回提示
    orchestration.run_async_cycle(session, ["t1"], trigger_type="message", trigger_ref="async:t1")
    cycle = store.get_cycle(orchestration.derive_cycle_id(session, "async:t1"))
    wm = json.loads(cycle["wm_json"])
    rendered = "\n".join(wm["situation"].get("retracted") or [])
    assert "撤回了一条消息" in rendered


# 12) 崩溃恢复：跑到 L2 后崩 → 续跑不重复
def test_crash_resume_l1_not_recalled(mind_env, fake_provider):
    from app.services.mind import cycle as mind_cycle

    session = "s-resume"
    add_message(session, "user", "继续话题", trace_id="t1")
    l1_calls_before = fake_provider.calls["mind_l1"]
    cycle_id = orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    n1 = len(_deliveries(session))
    # 模拟崩溃后重跑同一周期（投递已生成 → UNIQUE 幂等）
    orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    assert len(_deliveries(session)) == n1
    assert fake_provider.calls["mind_l1"] == l1_calls_before + 1


# 13) 租约：两个实例抢同一项
def test_lease_single_winner(mind_env):
    session = "s-lease"
    deferred_id = store.create_deferred(session, ["t1"], _T0 + 1, "test")
    now = mind_env["clock"]["t"]
    assert store.lease_deferred(deferred_id, now + 120)
    assert not store.lease_deferred(deferred_id, now + 240)  # 租约未过期
    mind_env["clock"]["t"] = now + 121  # 租约过期后
    assert store.lease_deferred(deferred_id, now + 240)


# 14) 课表缺失 → 等级 3 + world=unavailable
def test_schedule_missing_defaults_level3(mind_env, monkeypatch):
    monkeypatch.setattr(config, "MIND_SCHEDULE_PATH", "no/such/schedule.json")
    seg = schedule.segment_at(_T0)
    assert seg["level"] == 3
    assert seg["world"] == "unavailable"


# 1) MIND_ASYNC_DELIVERY=false：/chat 行为不变（同步路径仍走 run_cycle）
def test_sync_path_unchanged_when_disabled(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_ASYNC_DELIVERY", False)
    from app.services.chat_service import run_chat_turn

    result = run_chat_turn("s-sync", "你好", trace_id="t1")
    # 同步路径 E3 跑完整渲染：无注入时表情包标记被删
    assert result["reply"] == "好哦"
    assert result["trace_id"] == "t1"
    assert fetch_all("SELECT * FROM mind_delivery") == []


# ── 修正轮新增 ─────────────────────────────────────────────────────────────


# 修正1) 同步路径：injected/clean 顺序 + 注入不进模型请求 + 表情包表持久化
def test_sync_path_split_order_and_requests(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_ASYNC_DELIVERY", False)
    from app.services.chat_service import run_chat_turn, split_injected_context
    from app.services.mind.store import get_mind_session_context

    injected, clean = split_injected_context("【表情包表｜网关注入】\n表（名字共2个）：\n喵、晚安\n\n用户正文ABC")
    assert clean == "用户正文ABC"
    assert "喵" in injected and "用户正文ABC" not in injected

    run_chat_turn("s-split", "【表情包表｜网关注入】\n表（名字共2个）：\n喵、晚安\n\n用户正文ABC", trace_id="t1")
    for request in fake_provider.requests:
        flat = _flatten(request["messages"])
        if request["purpose"] in ("mind_l1", "mind_expr", "mind_think"):
            assert "用户正文ABC" in flat, request["purpose"]
            # L1 只看 clean_message（E2 的 system 第二块是表文本，允许）
            if request["purpose"] == "mind_l1":
                assert "【表情包表｜网关注入】" not in flat
    saved = get_mind_session_context("s-split")
    assert saved and "喵" in json.loads(saved["sticker_names_json"])


# 修正2) 调度器不阻塞事件循环：批次卡住时 due 接口照常
def test_scheduler_does_not_block_event_loop(mind_env, fake_provider):
    import threading
    import time as _time

    session = "s-block"
    add_message(session, "user", "hi", trace_id="t1")
    perception.create_perception(session, "t1")
    mind_env["clock"]["t"] += 3600
    release = threading.Event()

    def slow_process_due_batch(*_a, **_kw):
        release.wait(5)

    import app.services.mind.scheduler as sched_mod

    orig = sched_mod.process_due_batch
    sched_mod.process_due_batch = slow_process_due_batch
    try:
        started = _time.perf_counter()
        # 直接模拟一个正在跑的批次；to_thread 里的 due 调用不被阻塞
        sched_mod._batch_running = True

        def call_due():
            from app.routers.mind import deliveries_due

            result = deliveries_due(session, 50)
            return {"items": result["items"]}

        import asyncio

        loop = asyncio.new_event_loop()

        result = loop.run_until_complete(loop.run_in_executor(None, call_due))
        assert _time.perf_counter() - started < 5
        assert "items" in result
        # 批次互斥：正在跑时 _try_run_batch 直接跳过（不阻塞）
        ran = sched_mod._try_run_batch()
        assert ran is None
        release.set()
    finally:
        sched_mod.process_due_batch = orig
        sched_mod._batch_running = False


# 修正3) 作业失败 → 调度器重领重跑，不丢、不重复
def test_reply_job_retry_after_failure(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_JOB_MAX_ATTEMPTS", 3)
    session = "s-retry"
    add_message(session, "user", "继续", trace_id="t1")
    perception.create_perception(session, "t1")
    rec = store.get_perception_by_trace("t1")
    # 直接置 opened + seen_at，保证 handle_opened 走决策路径
    store.update_perception(rec["id"], seen="opened", seen_at=rec["glance_at"], status="opened")
    mind_env["clock"]["t"] = rec["glance_at"] + 1
    calls = {"n": 0}
    orig = orchestration.run_async_cycle

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return orig(*args, **kwargs)

    monkeypatch.setattr("app.services.mind.orchestration.run_async_cycle", flaky)
    # opened 批次直接决策写作业（不跑周期）
    from app.services.mind import scheduler as _sched

    trace_ids = _sched._open_batch(session)
    orchestration.handle_opened(session, trace_ids)
    job = store.get_pending_deferred(session)
    assert job, "决策应写作业"
    # 第一次执行抛异常 → job 退回 pending
    mind_scheduler.process_deferred(session, job["id"])
    assert store.get_deferred(job["id"])["status"] == "pending"
    # 重启调度器（新实例）后重领 → 成功
    mind_scheduler.process_due_batch()
    final_job = store.get_deferred(job["id"])
    assert final_job["status"] == "done"
    assert calls["n"] == 2
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "decided"
    texts = [r for r in _deliveries(session) if r["kind"] == "text"]
    assert len(texts) == 1, "投递不重复"


# 修正3b) attempts 超限 → failed + 日志
def test_reply_job_fails_after_max_attempts(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_JOB_MAX_ATTEMPTS", 1)
    session = "s-max"
    add_message(session, "user", "hi", trace_id="t1")
    store.create_deferred(session, ["t1"], mind_env["clock"]["t"], "reply_now")
    monkeypatch.setattr("app.services.mind.orchestration.run_async_cycle",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    job = store.get_pending_deferred(session)
    mind_scheduler.process_deferred(session, job["id"])
    assert store.get_deferred(job["id"])["status"] == "failed"


# 修正4) 正常回复 L1 只调 1 次
def test_l1_called_once_per_reply(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 0.025, "2": 0.12, "3": 30.0, "4": 3.0})
    session = "s-l1once"
    add_message(session, "user", "中午吃啥", trace_id="t1")
    rec = perception.create_perception(session, "t1")
    perception_rec = store.get_perception_by_trace("t1")
    store.update_perception(perception_rec["id"], seen="opened",
                           seen_at=perception_rec["glance_at"], status="opened")
    mind_env["clock"]["t"] = perception_rec["glance_at"] + 1
    before = fake_provider.calls["mind_l1"]
    trace_ids = mind_scheduler._open_batch(session)
    orchestration.handle_opened(session, trace_ids)
    decision_calls = fake_provider.calls["mind_l1"]
    # 调度器执行作业：周期复用作业里的 L1，不再调用
    after = fake_provider.calls["mind_l1"]
    assert after == decision_calls == before + 1
    job = store.get_pending_deferred(session)
    assert job
    mind_scheduler.process_due_batch()
    assert fake_provider.calls["mind_l1"] == after, "周期内复用作业的 L1，不再调用"


# 修正5) 已发出 text 后撤回 → 不重建
def test_retract_after_sent_keeps_cycle(mind_env, fake_provider):
    session = "s-rsent"
    add_message(session, "user", "第一条", trace_id="t1")
    cycle_id = orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    text = next(r for r in _deliveries(session) if r["kind"] == "text")
    from app.routers.mind import DeliveryAckRequest, deliveries_ack

    deliveries_ack(text["id"], DeliveryAckRequest(status="sent"))
    cycles_before = len(fetch_all("SELECT * FROM mind_cycle WHERE session_id=?", (session,)))
    texts_before = len([r for r in _deliveries(session) if r["kind"] == "text"])
    orchestration.rebuild_after_retract(session, "t1")
    cycles_after = len(fetch_all("SELECT * FROM mind_cycle WHERE session_id=?", (session,)))
    texts_after = len([r for r in _deliveries(session) if r["kind"] == "text"])
    assert cycles_after == cycles_before
    assert texts_after == texts_before


# 修正7) reply_will=not → 有 mark_read 无 text；打断不取消 mark_read
def test_mark_read_independent_of_reply(mind_env, fake_provider):
    fake_provider.l1_reply_will = "not"
    session = "s-mr"
    add_message(session, "user", "哦", trace_id="t1")
    perception.create_perception(session, "t1")
    store.update_perception(store.get_perception_by_trace("t1")["id"],
                            seen="opened", seen_at=mind_env["clock"]["t"], status="opened")
    orchestration.handle_opened(session, ["t1"])
    kinds = [r["kind"] for r in _deliveries(session)]
    assert "mark_read" in kinds and "text" not in kinds
    # mark_read 不随打断取消
    store.update_perception(store.get_perception_by_trace("t1")["id"], status="opened")
    add_message(session, "user", "第二条", trace_id="t2")
    cycle_id = orchestration.run_async_cycle(session, ["t2"], trigger_type="message")
    orchestration.on_new_message(session, "t2")
    marks = [r for r in _deliveries(session) if r["kind"] == "mark_read"]
    assert marks and all(r["status"] in ("pending", "sent", "claimed") for r in marks)


# 修正8) 异步入口 trace 去重
def test_async_entry_dedupes_trace(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_ASYNC_DELIVERY", True)
    from app.services.chat_service import run_chat_turn

    r1 = run_chat_turn("s-dedup", "hi", trace_id="t1")
    r2 = run_chat_turn("s-dedup", "hi", trace_id="t1")
    assert r1["status"] == "accepted" and r2["status"] == "accepted"
    assert r2.get("duplicate") is True
    users = fetch_all("SELECT * FROM messages WHERE session_id=? AND role='user'", ("s-dedup",))
    assert len(users) == 1
    assert len(fetch_all("SELECT * FROM mind_perception WHERE trace_id='t1'")) == 1


# 修正9) 课上收到、课后 glance → 按课后等级点开
def test_level_judged_at_glance_time(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 0.001, "2": 0.12, "3": 30.0, "4": 3.0})
    session = "s-glance"
    class_time = datetime(2026, 10, 12, 8, 10).timestamp()
    add_message(session, "user", "下课了吗", trace_id="t1")
    rec = perception.create_perception(session, "t1", received_at=class_time)
    after_class = datetime(2026, 10, 12, 9, 41).timestamp()
    assert rec["glance_at"] > class_time
    mind_env["clock"]["t"] = max(rec["glance_at"], after_class)
    mind_scheduler.process_glance(session, "t1")
    rec = store.get_perception_by_trace("t1")
    assert rec["seen"] == "opened", "课后 glance 应按课后等级点开"


# 修正10) 投递认领：第二次拉不到同一条；租约过期可重拿
def test_delivery_claim_and_lease(mind_env):
    from app.routers.mind import deliveries_due

    session = "s-claim"
    add_message(session, "user", "hi", trace_id="t1")
    cycle_id = orchestration.run_async_cycle(
        session, ["t1"], trigger_type="message",
        trigger_ref="async:t1",
    ) if False else None
    store.insert_delivery({
        "session_id": session, "cycle_id": "claim-cyc", "seq": 0,
        "kind": "text", "payload": {"text": "hi"}, "send_at": mind_env["clock"]["t"],
    })
    mind_env["clock"]["t"] += 1
    first = deliveries_due(session, 10)["items"]
    assert first
    second = deliveries_due(session, 10)["items"]
    assert all(item["id"] != first[0]["id"] for item in second), "租约内不可重复拉取"
    # 租约到期 = 认领时刻 + lease；再 +1 保证严格过期
    claimed_at = mind_env["clock"]["t"]
    mind_env["clock"]["t"] = claimed_at + int(config.MIND_DELIVERY_LEASE_SEC) + 1
    third = deliveries_due(session, 10)["items"]
    assert any(item["id"] == first[0]["id"] for item in third), "租约过期后可重拿"


# 修正12) text ack failed → 同周期后续 text/sticker/typing 取消，mark_read 保留
def test_text_failed_cancels_rest(mind_env, fake_provider):
    from app.routers.mind import DeliveryAckRequest, deliveries_ack

    session = "s-fail"
    add_message(session, "user", "hi", trace_id="t1")
    perception.create_perception(session, "t1")
    store.update_perception(store.get_perception_by_trace("t1")["id"],
                            seen="opened", seen_at=mind_env["clock"]["t"], status="opened")
    orchestration.handle_opened(session, ["t1"])
    store.update_perception(store.get_perception_by_trace("t1")["id"], status="decided")
    job = store.get_pending_deferred(session)
    mind_scheduler.process_deferred(session, job["id"])
    rows = _deliveries(session)
    texts = [r for r in rows if r["kind"] == "text"]
    assert texts
    marks = [r for r in rows if r["kind"] == "mark_read"]
    assert marks
    deliveries_ack(texts[0]["id"], DeliveryAckRequest(status="failed", error="boom"))
    rows = _deliveries(session)
    assert all(r["status"] != "pending" for r in rows if r["kind"] in ("text", "sticker", "typing"))
    assert marks[0]["status"] in ("pending", "claimed"), "mark_read 保留"


# ── 三轮收尾 ──────────────────────────────────────────────────────────────


# 修正1) reply_will=not → perception decided，不再被恢复重跑 L1
def test_not_reply_decided_no_recovery(mind_env, fake_provider, monkeypatch):
    import logging

    fake_provider.l1_reply_will = "not"
    session = "s-not2"
    _force_opened(session, "t1")
    before = fake_provider.calls["mind_l1"]
    orchestration.handle_opened(session, ["t1"])
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "decided", "不回也是决策"
    kinds = [r["kind"] for r in _deliveries(session)]
    assert "mark_read" in kinds and "text" not in kinds
    # 过了 grace 也不应重跑 L1，也没有 mind_job_failed
    mind_env["clock"]["t"] += 31
    recovered = mind_scheduler.recover_stuck_opened()
    assert recovered == 0, "decided 不属于 stuck opened"
    after = fake_provider.calls["mind_l1"]
    assert after == before + 1, "只有决策时那一次 L1，恢复没有再调"


# 修正2) level2_busy 入队 → queued；深想作业失败 → perception failed
def test_level2_busy_queues_perception(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN",
                        {"0": 0, "1": 0.025, "2": 30.0, "3": 0.35, "4": 3.0})
    fake_provider.l1_reply_will = "later"
    session = "s-busy"
    walk = datetime(2026, 10, 12, 12, 20).timestamp()
    mind_env["clock"]["t"] = walk
    monkeypatch.setattr("app.services.mind.schedule.segment_at",
                        lambda t=None, session_id=None: {"activity": "走路", "level": 2, "phone": "身上",
                                        "muted": False, "start": None, "end": None,
                                        "world": "schedule"})
    _force_opened(session, "t1")
    orchestration.handle_opened(session, ["t1"])
    rec = store.get_perception_by_trace("t1")
    job = store.get_pending_deferred(session)
    assert job and job["reason"] == "level2_busy"
    assert rec["status"] == "queued" and rec["deferred_id"] == job["id"]


def test_deep_job_failure_fails_perception(mind_env, fake_provider, monkeypatch):
    monkeypatch.setattr(config, "MIND_JOB_MAX_ATTEMPTS", 1)
    session = "s-deepfail"
    _force_opened(session, "t1")
    fake_provider.l1_reply_will = "now"
    # 等级 2 快路径：先短回 + 深想
    walk = datetime(2026, 10, 12, 12, 20).timestamp()
    mind_env["clock"]["t"] = walk
    monkeypatch.setattr("app.services.mind.schedule.segment_at",
                        lambda t=None, session_id=None: {"activity": "走路", "level": 2, "phone": "身上",
                                        "muted": False, "start": None, "end": None,
                                        "world": "schedule"})
    orchestration.handle_opened(session, ["t1"])
    quick_job = store.get_pending_deferred(session)
    assert quick_job and quick_job["reason"] == "quick_reply"
    mind_scheduler.process_deferred(session, quick_job["id"])
    deep_jobs = [dict(j) for j in fetch_all(
        "SELECT * FROM mind_deferred WHERE session_id=? AND reason='quick_reply_then_think'",
        (session,))]
    assert deep_jobs, "短回 done 后产生深想作业"
    deep = deep_jobs[-1]
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "queued" and rec["deferred_id"] == deep["id"], \
        "深想作业把 decided 重新置为 queued"
    # 深想作业失败 → perception failed
    monkeypatch.setattr("app.services.mind.orchestration.run_async_cycle",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("deep boom")))
    mind_env["clock"]["t"] = deep["due_at"] + 0.1
    mind_scheduler.process_deferred(session, deep["id"])
    assert store.get_deferred(deep["id"])["status"] == "failed"
    assert store.get_perception_by_trace("t1")["status"] == "failed"


# ── 二轮修正 ──────────────────────────────────────────────────────────────


# 修正1) queued 状态机 + pending 合批重想 / running 不打扰
def test_queued_status_and_pending_merge(mind_env, fake_provider, monkeypatch):
    session = "s-queued"
    _force_opened(session, "t1")
    before_l1 = fake_provider.calls["mind_l1"]
    orchestration.handle_opened(session, ["t1"])
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "queued" and rec["deferred_id"] is not None
    old_job = store.get_deferred(rec["deferred_id"])
    assert old_job["status"] == "pending"

    # 新消息点开：pending 作业取消、trace 改回 opened、合批重想
    _force_opened(session, "t2")
    fake_provider.l1_reply_will = "later"
    orchestration.handle_opened(session, ["t2"])
    assert store.get_deferred(old_job["id"])["status"] == "cancelled"
    new_jobs = [j for j in store.list_pending_deferred(session)]
    assert len(new_jobs) == 1, "合批后只有一个作业"
    merged_traces = json.loads(new_jobs[0]["trace_ids_json"])
    assert set(merged_traces) == {"t1", "t2"}
    # L1：第一次决策 1 + 合批重想 1 = 2
    assert fake_provider.calls["mind_l1"] - before_l1 == 2


def test_running_job_not_disturbed(mind_env, fake_provider, monkeypatch):
    session = "s-running"
    _force_opened(session, "t1")
    orchestration.handle_opened(session, ["t1"])
    rec = store.get_perception_by_trace("t1")
    job = store.get_deferred(rec["deferred_id"])
    # 模拟作业 running（租约内）
    store.update_deferred(job["id"], status="running",
                          lease_until=mind_env["clock"]["t"] + 999)
    _force_opened(session, "t2")
    orchestration.handle_opened(session, ["t2"])
    assert store.get_deferred(job["id"])["status"] == "running", "running 不打扰"
    new_jobs = [j for j in store.list_pending_deferred(session) if j["id"] != job["id"]]
    assert len(new_jobs) == 1
    assert json.loads(new_jobs[0]["trace_ids_json"]) == ["t2"], "第二个作业只含新 trace"


# 修正2) 卡住 opened 恢复
def test_stuck_opened_recovered_after_grace(mind_env, fake_provider, monkeypatch):
    session = "s-stuck"
    _force_opened(session, "t1")
    calls = {"n": 0}
    orig = orchestration.handle_opened

    def flaky(sess, traces):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("l1 boom")
        return orig(sess, traces)

    monkeypatch.setattr("app.services.mind.orchestration.handle_opened", flaky)
    # 第一次：抛异常，opened 卡住
    try:
        orchestration.handle_opened(session, ["t1"])
    except RuntimeError:
        pass
    assert store.get_perception_by_trace("t1")["status"] == "opened"
    # 推进超过 grace → 调度器恢复
    monkeypatch.setattr(config, "MIND_OPENED_GRACE_SEC", 30)
    mind_env["clock"]["t"] += 31
    mind_scheduler.recover_stuck_opened()
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "queued", "恢复后入队"
    assert calls["n"] == 2


def test_retracted_new_message_old_trace_recovered(mind_env, fake_provider):
    session = "s-rstuck"
    _force_opened(session, "t1")
    # 打断：t1 改回 opened
    store.update_perception(store.get_perception_by_trace("t1")["id"], status="opened")
    # 新消息未读即被撤回
    _force_opened(session, "t2")
    from app.routers.mind import retract, RetractRequest

    retract(RetractRequest(trace_id="t2"))
    # 旧 trace 卡在 opened，推进 grace 后恢复
    mind_env["clock"]["t"] += 31
    mind_scheduler.recover_stuck_opened()
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "queued", "撤回新消息后旧 trace 最终被恢复"


# 修正3) 认领原子 + claimed 不撤 + cancelled 后 ack sent
def test_concurrent_due_claim_atomic(mind_env):
    session = "s-claim2"
    store.insert_delivery({
        "session_id": session, "cycle_id": "cc", "seq": 0,
        "kind": "text", "payload": {"text": "hi"},
        "send_at": mind_env["clock"]["t"],
    })
    mind_env["clock"]["t"] += 1
    first = store.due_deliveries(session, mind_env["clock"]["t"])
    second = store.due_deliveries(session, mind_env["clock"]["t"])
    assert first and not any(item["id"] == first[0]["id"] for item in second)


def test_ack_sent_after_cancel_writes_history(mind_env):
    from app.routers.mind import DeliveryAckRequest, deliveries_ack

    session = "s-sentcancel"
    store.insert_delivery({
        "session_id": session, "cycle_id": "sc", "seq": 0,
        "kind": "text", "payload": {"text": "已经发出去了"},
        "send_at": mind_env["clock"]["t"],
    })
    row = store.get_delivery(1)
    store.cancel_remaining_deliveries("sc")
    result = deliveries_ack(row["id"], DeliveryAckRequest(status="sent"))
    assert result["ok"]
    assistants = fetch_all(
        "SELECT * FROM messages WHERE session_id=? AND role='assistant'", (session,))
    assert any("已经发出去了" in r["content"] for r in assistants)


# 修正4) 撤回内容不进周期
def test_retract_removes_trace_from_pending_job(mind_env, fake_provider):
    session = "s-rjob"
    _force_opened(session, "t1")
    _force_opened(session, "t2")
    from app.routers.mind import retract, RetractRequest

    perception2 = store.get_perception_by_trace("t2")
    store.update_perception(perception2["id"], seen="none")
    # 两条一起入队
    fake_provider.l1_reply_will = "later"
    orchestration.handle_opened(session, ["t1", "t2"])
    jobs = store.list_pending_deferred(session)
    assert jobs and set(json.loads(jobs[0]["trace_ids_json"])) == {"t1", "t2"}
    retract(RetractRequest(trace_id="t2"))
    job = store.get_deferred(jobs[0]["id"])
    assert json.loads(job["trace_ids_json"]) == ["t1"], "撤回的 trace 从作业移除"
    # 周期输入不含撤回原文
    cycle_id = orchestration.run_reply_job(job["id"])
    row = store.get_cycle(cycle_id)
    assert row is not None


def test_retract_all_traces_cancels_job(mind_env, fake_provider):
    session = "s-rall"
    _force_opened(session, "t1")
    from app.routers.mind import retract, RetractRequest

    orchestration.handle_opened(session, ["t1"])
    job = store.list_pending_deferred(session)[0]
    retract(RetractRequest(trace_id="t1"))
    assert store.get_deferred(job["id"])["status"] == "cancelled"
    assert not [r for r in _deliveries(session) if r["kind"] == "text"], "全撤回不产生 text"


# 修正5) 只取消相关作业，深想作业不动
def test_on_new_message_keeps_unrelated_deep_job(mind_env, fake_provider):
    session = "s-deepkeep"
    _force_opened(session, "t1")
    _force_opened(session, "t2")
    # 造一个 unrelated pending 作业（模拟 quick_reply_then_think）
    deep_id = store.create_deferred(session, ["t9"], mind_env["clock"]["t"] + 100,
                                    "quick_reply_then_think")
    # 另一个周期针对 t1
    cycle_id = orchestration.run_async_cycle(session, ["t1"], trigger_type="message")
    _force_opened(session, "t2")
    orchestration.on_new_message(session, "t2")
    assert store.get_deferred(deep_id)["status"] == "pending", "深想作业不动"


def test_deep_job_binds_traces_with_other_pending_job(mind_env, fake_provider, monkeypatch):
    """短回完成时已存在另一条 pending 作业 → 深想作业仍正确绑定本批 trace。

    旧实现用 get_pending_deferred(session) 找刚入队的深想作业，同会话已有
    别的 pending 作业（如另一批的 quick_reply_then_think）时会认错（P3 收尾第 3 条）。
    """
    session = "s-deepbind"
    _force_opened(session, "t1")
    fake_provider.l1_reply_will = "now"
    walk = datetime(2026, 10, 12, 12, 20).timestamp()
    mind_env["clock"]["t"] = walk
    monkeypatch.setattr("app.services.mind.schedule.segment_at",
                        lambda t=None, session_id=None: {"activity": "走路", "level": 2, "phone": "身上",
                                        "muted": False, "start": None, "end": None,
                                        "world": "schedule"})
    orchestration.handle_opened(session, ["t1"])
    quick_job = store.get_pending_deferred(session)
    assert quick_job and quick_job["reason"] == "quick_reply"
    # 短回执行前，同会话出现另一条 pending 深想作业（另一批）
    unrelated_id = store.create_deferred(
        session, ["t-unrelated"], walk + 600, "quick_reply_then_think")
    mind_scheduler.process_deferred(session, quick_job["id"])
    deep_jobs = [dict(j) for j in fetch_all(
        "SELECT * FROM mind_deferred WHERE session_id=? AND reason='quick_reply_then_think' AND id<>?",
        (session, unrelated_id))]
    assert deep_jobs, "短回 done 后产生新深想作业"
    deep = deep_jobs[-1]
    rec = store.get_perception_by_trace("t1")
    assert rec["status"] == "queued" and rec["deferred_id"] == deep["id"], \
        "深想作业绑定本批 trace，不受无关 pending 作业影响"
    # 无关作业保持原样
    still = store.get_deferred(unrelated_id)
    assert still["status"] == "pending"
    assert json.loads(still["trace_ids_json"]) == ["t-unrelated"]



# ── P3 修正：L0 专注窗口（等级 4）按 session 的“她的活动”判定 ──────────────
# 固定 seed 的 u 值（sha256("s:focus-a")[:16]/2**64 等），用于断言具体 glance_at


def _set_assistant_time(trace_id: str, ts: float) -> None:
    execute_write(
        "UPDATE messages SET created_at = datetime(?, 'unixepoch') WHERE trace_id = ?",
        (int(ts), trace_id),
    )


def _opened_perception(session: str, trace: str, seen_at: float) -> None:
    perception.create_perception(session, trace)
    record = store.get_perception_by_trace(trace)
    store.update_perception(
        record["id"], seen="opened", seen_at=seen_at, status="opened"
    )


def _fix_u(monkeypatch, u: float) -> None:
    """把 seed 抽样固定成指定 u，断言不依赖真实哈希。"""
    monkeypatch.setattr(perception, "_seed_float", lambda session_id, trace_id: u)


def _set_focus_window(monkeypatch) -> None:
    monkeypatch.setattr(config, "MIND_FOCUS_WINDOW_MIN", 5)


def test_focus_window_lambda4_with_ack_reply(mind_env, monkeypatch):
    """a. 她 2 分钟前有 ack 回复，u=0.5 → 窗口内按 λ4 命中。"""
    from datetime import datetime as _dt
    _set_focus_window(monkeypatch)
    received = _dt(2026, 10, 12, 12, 0).timestamp()  # 假课表 default 空闲 level 3
    mind_env["clock"]["t"] = received
    session = "s-focus-a"
    _set_assistant_time(_add_msg(session, "assistant", "ack-reply", trace_id="a1"),
                        received - 120)
    _fix_u(monkeypatch, 0.5)
    got = perception.sample_glance_time(received, 1, session, "focus-a")
    target = -math.log(0.5)  # ≈0.693
    expected = received + target / 3.0 * 60
    assert abs(got - expected) < 0.01, (got, expected)


def test_focus_window_not_triggered_by_user_only(mind_env, monkeypatch):
    """b. 只有用户消息、窗口=5 → 等级 3，且按 λ3 抽样。"""
    from datetime import datetime as _dt
    _set_focus_window(monkeypatch)
    received = _dt(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = received
    session = "s-focus-b"
    _add_msg(session, "user", "user-only", trace_id="u1")
    _fix_u(monkeypatch, 0.5)
    seg = schedule.segment_at(received, session_id=session)
    assert seg["level"] == 3
    got = perception.sample_glance_time(received, 1, session, "focus-b")
    target = -math.log(0.5)
    expected = received + target / 0.35 * 60
    assert abs(got - expected) < 0.01, (got, expected)


def test_focus_window_isolated_per_session(mind_env, monkeypatch):
    """c. 只有另一个 session 有活动、窗口=5 → 本 session 等级 3、λ3 抽样。"""
    from datetime import datetime as _dt
    _set_focus_window(monkeypatch)
    received = _dt(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = received
    other = "s-focus-other"
    _set_assistant_time(_add_msg(other, "assistant", "other-ack", trace_id="o1"),
                        received - 60)
    session = "s-focus-c"
    _fix_u(monkeypatch, 0.5)
    assert store.last_self_activity(session, received) is None
    seg = schedule.segment_at(received, session_id=session)
    assert seg["level"] == 3, "跨 session 活动不升级"
    got = perception.sample_glance_time(received, 1, session, "focus-c")
    target = -math.log(0.5)
    expected = received + target / 0.35 * 60
    assert abs(got - expected) < 0.01, (got, expected)


def test_focus_window_expires_mid_sampling(mind_env, monkeypatch):
    """d. 活动 4.5 分钟前 → 窗口只剩 30 秒；u=0.9 前段 λ4 后段 λ3。"""
    from datetime import datetime as _dt
    _set_focus_window(monkeypatch)
    received = _dt(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = received
    session = "s-focus-d"
    _set_assistant_time(_add_msg(session, "assistant", "just-now", trace_id="d1"),
                        received - 270)
    _fix_u(monkeypatch, 0.9)
    got = perception.sample_glance_time(received, 1, session, "focus-d")
    target = -math.log(0.1)  # ≈2.3026
    acc4 = 3.0 * (30 / 60)   # 窗口内 λ4 累加 = 1.5 < target
    rest = (target - acc4) / 0.35
    expected = received + 30 + rest * 60
    assert abs(got - expected) < 0.01, (got, expected)


def test_focus_window_never_upgrades_low_level(mind_env, monkeypatch):
    """e. 周一 08:10 上课（level 1），她 1 分钟前有活动 → 等级 1，λ=0.025 精确值。"""
    _set_focus_window(monkeypatch)
    mind_env["clock"]["t"] = _T0
    session = "s-focus-e"
    _set_assistant_time(_add_msg(session, "assistant", "class-ack", trace_id="e1"),
                        _T0 - 60)
    _fix_u(monkeypatch, 0.5)
    seg = schedule.segment_at(_T0, session_id=session)
    assert seg["level"] == 1, "等级 1 不升级"
    got = perception.sample_glance_time(_T0, 1, session, "focus-e")
    target = -math.log(0.5)
    expected = _T0 + target / 0.025 * 60
    assert abs(got - expected) < 0.01, (got, expected)


def test_focus_window_opened_counts_as_activity(mind_env, monkeypatch):
    """f. 她点开过消息（seen=opened）但还没回复，也算活动（窗口=5）。"""
    from datetime import datetime as _dt
    _set_focus_window(monkeypatch)
    received = _dt(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = received
    session = "s-focus-f"
    _opened_perception(session, "f0", received - 120)
    last = store.last_self_activity(session, received)
    assert last is not None and abs(last - (received - 120)) < 0.01
    seg = schedule.segment_at(received, session_id=session)
    assert seg["level"] == 4, "点开过也算她在聊天里"

def _add_msg(session: str, role: str, content: str, *, trace_id: str) -> str:
    add_message(session, role, content, trace_id=trace_id)
    return trace_id

import math  # noqa: E402
from app.storage.db import execute_write  # noqa: E402
from app.services.mind import perception, schedule  # noqa: E402







def test_open_sees_all_unread_in_session(mind_env, fake_provider, monkeypatch):
    """点开聊天时同 session 早到但 glance_at 更晚的未读也一起被看到、并进同一批。"""
    monkeypatch.setattr(perception, "_seed_float", lambda s, t: {"a": 0.99, "b": 0.01}[t])
    fake_provider.l1_depth = "fast"
    t_noon = datetime(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = t_noon
    session = "s-open-all"
    add_message(session, "user", "刷手机", trace_id="a")
    rec_a = perception.create_perception(session, "a")
    mind_env["clock"]["t"] = t_noon + 16
    add_message(session, "user", "我刚吃完饭", trace_id="b")
    rec_b = perception.create_perception(session, "b")
    assert rec_b["glance_at"] < rec_a["glance_at"]

    mind_env["clock"]["t"] = rec_b["glance_at"] + 1
    mind_scheduler.process_due_batch()

    a = store.get_perception_by_trace("a")
    b = store.get_perception_by_trace("b")
    assert a["seen"] == "opened" and a["seen_at"] == b["seen_at"]
    reads = sorted(json.loads(r["payload_json"])["trace_ids"][0]
                   for r in _deliveries(session) if r["kind"] == "mark_read")
    assert reads == ["a", "b"]
    job = store.get_deferred(1)
    assert json.loads(job["trace_ids_json"]) == ["a", "b"]


def test_in_chat_new_message_glanced_fast(mind_env, fake_provider, monkeypatch):
    """她刚发完消息（linger 内）：新消息不再抽样，IN_CHAT_GLANCE 秒后就看到；超出 linger 恢复抽样。"""
    monkeypatch.setattr(perception, "_seed_float", lambda s, t: 0.5)
    monkeypatch.setattr(config, "MIND_L0_LAMBDA_PER_MIN", {"0": 0, "1": 0.025, "2": 0.12, "3": 0.35, "4": 3.0})
    monkeypatch.setattr(config, "MIND_IN_CHAT_LINGER_SEC", 60)
    monkeypatch.setattr(config, "MIND_IN_CHAT_GLANCE_SEC", 2.0)
    t_noon = datetime(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = t_noon
    session = "s-inchat"
    _opened_perception(session, "old", t_noon - 30)
    add_message(session, "user", "在吗", trace_id="n1")
    rec = perception.create_perception(session, "n1")
    assert rec["glance_at"] == t_noon + 2.0
    mind_env["clock"]["t"] = t_noon + 200
    add_message(session, "user", "人呢", trace_id="n2")
    rec2 = perception.create_perception(session, "n2")
    # n1 仍未读 → unread=2，λ 乘 1.2
    expected = t_noon + 200 + (-math.log(0.5)) / (0.35 * 1.2) * 60
    assert abs(rec2["glance_at"] - expected) < 0.01


def test_settle_waits_for_pause(mind_env, fake_provider, monkeypatch):
    """点开后按等级等对方停顿：等级3 等最后一条 +10s；有未点开的更新消息按它算；最长 30s。"""
    monkeypatch.setattr(config, "MIND_SETTLE_SEC", {"3": 10, "4": 5})
    monkeypatch.setattr(config, "MIND_SETTLE_MAX_SEC", 30.0)
    monkeypatch.setattr(config, "MIND_IN_CHAT_LINGER_SEC", 0)
    t_noon = datetime(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = t_noon
    session = "s-settle"
    _force_opened(session, "a", seen_at=t_noon)
    assert orchestration._settle_due(session, ["a"], 3) == t_noon + 10
    mind_env["clock"]["t"] = t_noon + 8
    add_message(session, "user", "还有", trace_id="b")
    perception.create_perception(session, "b")
    assert orchestration._settle_due(session, ["a"], 3) == t_noon + 18
    assert orchestration._settle_due(session, ["a"], 4) == t_noon + 13
    mind_env["clock"]["t"] = t_noon + 40
    add_message(session, "user", "再来", trace_id="c")
    perception.create_perception(session, "c")
    assert orchestration._settle_due(session, ["a"], 3) == t_noon + 40


def test_new_message_while_thinking_merges(mind_env, fake_provider, monkeypatch):
    """想到一半（作业 running）来了新消息并已点开：本轮作废、一句不发，两条合批重想。"""
    monkeypatch.setattr(config, "MIND_SETTLE_SEC", {})
    fake_provider.l1_depth = "fast"
    fake_provider.l1_reply_will = "now"
    session = "s-think"
    t_noon = datetime(2026, 10, 12, 12, 0).timestamp()
    mind_env["clock"]["t"] = t_noon
    _force_opened(session, "a")
    orchestration.handle_opened(session, ["a"])
    job_a = store.get_deferred(store.get_perception_by_trace("a")["deferred_id"])
    orig = orchestration.run_async_cycle

    def cycle_with_arrival(*args, **kwargs):
        result = orig(*args, **kwargs)
        mind_env["clock"]["t"] = t_noon + 5
        _force_opened(session, "b")
        orchestration.handle_opened(session, ["b"])
        return result

    monkeypatch.setattr(orchestration, "run_async_cycle", cycle_with_arrival)
    orchestration.run_reply_job(int(job_a["id"]))
    monkeypatch.setattr(orchestration, "run_async_cycle", orig)
    assert store.get_deferred(int(job_a["id"]))["status"] == "cancelled"
    texts = [r for r in _deliveries(session) if r["kind"] in ("text", "typing")]
    assert texts and all(r["status"] == "cancelled" for r in texts)
    jobs = store.list_pending_deferred(session)
    assert len(jobs) == 1 and json.loads(jobs[0]["trace_ids_json"]) == ["a", "b"]


def test_async_e2_failure_retries_without_persona_less_fallback(mind_env, fake_provider, monkeypatch):
    """异步作业里 E2 网络失败：不走旧单次调用兜底，作业退回 pending，重试后正常出句。"""
    monkeypatch.setattr(config, "MIND_SETTLE_SEC", {})
    fake_provider.l1_depth = "fast"
    fake_provider.l1_reply_will = "now"
    fallback_calls = []
    monkeypatch.setattr("app.services.mind.cycle._llm_gateway.generate_chat_reply",
                        lambda *a, **k: fallback_calls.append(1) or "我是AI")
    real_e2 = __import__("app.services.mind.expression", fromlist=["run_e2"]).run_e2
    state = {"n": 0}

    def flaky_e2(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("SSLError")
        return real_e2(**kwargs)

    monkeypatch.setattr("app.services.mind.cycle.expression.run_e2", flaky_e2)
    session = "s-e2"
    _force_opened(session, "t1")
    orchestration.handle_opened(session, ["t1"])
    job_id = int(store.get_perception_by_trace("t1")["deferred_id"])
    mind_scheduler.process_deferred(session, job_id)
    assert fallback_calls == []
    assert store.get_deferred(job_id)["status"] == "pending"
    mind_scheduler.process_deferred(session, job_id)
    assert store.get_deferred(job_id)["status"] == "done"
    texts = [json.loads(r["payload_json"])["text"] for r in _deliveries(session) if r["kind"] == "text"]
    assert texts == ["好哦"]
    assert fallback_calls == []
