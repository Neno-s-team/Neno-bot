"""P3a 编排：MIND_ASYNC_DELIVERY=true 时的消息入口与回复决策。

/chat 同步路径与异步路径共用本模块，保证 MIND_ASYNC_DELIVERY=false 时
行为与 P2 完全一致。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app import config
from app.services.mind import clock, cycle, deferred, delivery, perception, schedule, store
from app.services.mind.schemas import L1Appraisal
from app.storage.db import fetch_all, get_recent_messages_by_tokens
from app.utils.logging_utils import log_event


def derive_cycle_id(session_id: str, key: str) -> str:
    return hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:32]


def _load_batch_text(session_id: str, trace_ids: list[str]) -> str:
    """按 trace 顺序取出消息全文并聚合；trace 查不到时退回 user_records 内容。"""
    parts: list[str] = []
    for trace_id in trace_ids:
        rows = fetch_all(
            "SELECT content FROM messages WHERE session_id = ? AND trace_id = ? AND role = 'user'"
            " ORDER BY id ASC",
            (session_id, trace_id),
        )
        text = "\n".join(row["content"] for row in rows).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def run_async_cycle(
    session_id: str,
    trace_ids: list[str],
    *,
    trigger_type: str = "deferred",
    trigger_ref: str | None = None,
    injected_context: str = "",
    skip_l2: bool = False,
    force_short: bool = False,
    max_messages: int | None = None,
    plan_override: dict[str, Any] | None = None,
) -> str:
    """对一个聚合批跑完整认知周期并生成投递计划。

    返回 cycle_id；投递计划已落库（幂等），assistant 历史由 ack 写入。
    """
    if not trace_ids:
        raise ValueError("run_async_cycle requires at least one trace_id")
    message = _load_batch_text(session_id, trace_ids)
    history = get_recent_messages_by_tokens(
        session_id, token_limit=max(1000, config.HISTORY_TOKEN_LIMIT)
    )
    clean_message = message
    key = trigger_ref or f"async:{':'.join(trace_ids)}"
    cycle_id = derive_cycle_id(session_id, key)
    if not trigger_ref:
        trigger_ref = key

    plan_patch = None
    if force_short or max_messages is not None:
        plan_patch = {"length": "short" if force_short else None,
                      "n_messages": max_messages}
        plan_patch = {k: v for k, v in plan_patch.items() if v is not None}
    if plan_override:
        plan_patch = {**(plan_patch or {}), **plan_override}

    final_text = cycle.run_cycle(
        session_id,
        clean_message,
        history,
        messages=[],  # 异步路径不回退旧 generate_chat_reply（仍生成投递计划）
        trace_id=trace_ids[0],
        injected_context=injected_context,
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        skip_l2=skip_l2,
        plan_patch=plan_patch,
    )
    if not final_text.strip():
        return cycle_id

    existing = store.get_cycle(cycle_id) or {}
    hesitate = False
    try:
        l2_json = existing.get("l2_json")
        if l2_json:
            hesitate = bool(json.loads(l2_json).get("l3", {}).get("hesitate", False))
    except Exception:
        hesitate = False
    # mark_read 只覆盖"看到过"的消息
    seen_trace_ids = [
        str(row["trace_id"])
        for row in fetch_all(
            "SELECT DISTINCT trace_id FROM messages WHERE session_id = ? AND trace_id IN (%s)"
            % ",".join("?" for _ in trace_ids),
            (session_id, *trace_ids),
        )
        if row["trace_id"] and _was_seen(session_id, str(row["trace_id"]))
    ]
    decision_ts = float(existing.get("updated_at") or clock.now())
    # 投递计划基于决策时刻，但 send_at 不能早于收到时刻（没读之前不会发）
    delivery.plan_deliveries(
        session_id,
        final_text,
        cycle_id=cycle_id,
        base_ts=decision_ts,
        trace_ids=seen_trace_ids,
        hesitate=hesitate,
    )
    return cycle_id


def _was_seen(session_id: str, trace_id: str) -> bool:
    record = store.get_perception_by_trace(trace_id)
    return bool(record and record["seen"] in ("preview", "opened") and not record["retracted"])


def handle_glanced(session_id: str, trace_ids: list[str]) -> None:
    """到点（等级>=2 打开）：用全文重跑 L1 并做回复决策。"""
    from app.services.mind import appraisal, state as mind_state

    if not trace_ids:
        return
    message = _load_batch_text(session_id, trace_ids)
    if not message:
        return
    seg = schedule.segment_at(clock.now())
    level = int(seg.get("level", 3))
    mind = mind_state.read_state(session_id)
    l1 = appraisal.run_l1(
        message=message,
        state=mind,
        self_card="",
        recent_turns=[],
        trace_id=trace_ids[0],
        session_id=session_id,
    )
    _decide(session_id, trace_ids, l1, level)


def _decide(session_id: str, trace_ids: list[str], l1: L1Appraisal, level: int) -> None:
    if l1.reply_will == "not":
        # 记一个 not_reply 周期的完成态，不进队列
        key = f"decision:not:{':'.join(trace_ids)}"
        cycle_id = derive_cycle_id(session_id, key)
        store.create_cycle(cycle_id, session_id, trigger_type="decision", trigger_ref=key)
        store.update_cycle(cycle_id, status="completed", error=None)
        log_event("mind", "decision_not_reply", trace_id=trace_ids[0], session_id=session_id)
        return
    if level == 2:
        if l1.depth == "fast":
            run_async_cycle(
                session_id, trace_ids,
                trigger_type="message", skip_l2=True, force_short=True, max_messages=2,
            )
            return
        if l1.reply_will == "now":
            # 先短回一条，同时进待想队列等深想
            run_async_cycle(
                session_id, trace_ids,
                trigger_type="message", skip_l2=True, force_short=True, max_messages=1,
            )
            due_at = deferred.compute_due_at(clock.now(), min_level=3)
            deferred.enqueue(session_id, trace_ids, due_at, reason="quick_reply_then_think")
            return
        due_at = deferred.compute_due_at(clock.now(), min_level=3)
        deferred.enqueue(session_id, trace_ids, due_at, reason="level2_busy")
        return
    # 等级 >=3：完整周期（P2）
    run_async_cycle(session_id, trace_ids, trigger_type="message")


def handle_opened(session_id: str, trace_ids: list[str]) -> None:
    """调度器驱动：点开的批次走回复决策。"""
    handle_glanced(session_id, trace_ids)


def on_new_message(session_id: str, trace_id: str) -> None:
    """新消息到达：text 一条都没发出的周期 → cancel 并合并成一个新周期。"""
    if not config.MIND_ASYNC_DELIVERY:
        return
    for row in store.list_pending_delivery_cycles(session_id):
        cycle_id = row["cycle_id"]
        if store.cycle_has_sent_text(cycle_id):
            continue  # 已经发出一条以上 → 让它发完
        trace_ids = [item for item in _pending_cycle_trace_ids(cycle_id) if item in _seen_opened_ids(session_id)]
        store.cancel_remaining_deliveries(cycle_id)
        for item in _seen_opened_ids(session_id):
            if item not in trace_ids:
                trace_ids.append(item)
        if trace_ids:
            due_at = clock.now()
            deferred.enqueue(session_id, trace_ids, due_at, reason="interrupted")
        log_event("mind", "delivery_cycle_cancelled", session_id=session_id, cycle_id=cycle_id)


def _seen_opened_ids(session_id: str) -> list[str]:
    return [
        str(row["trace_id"])
        for row in store.pending_perceptions(session_id)
        if row["seen"] == "opened"
    ]


def rebuild_after_retract(session_id: str, trace_id: str) -> None:
    """撤回已看过的消息：取消该消息所在周期的剩余投递，重跑一个不含它的周期。"""
    if not config.MIND_ASYNC_DELIVERY:
        return
    for row in store.list_pending_delivery_cycles(session_id):
        cycle_id = row["cycle_id"]
        trace_ids = _pending_cycle_trace_ids(cycle_id)
        if trace_id not in trace_ids:
            continue
        if not store.cycle_has_sent_text(cycle_id):
            store.cancel_remaining_deliveries(cycle_id)
        trace_ids = [item for item in trace_ids if item != trace_id]
        if trace_ids:
            run_async_cycle(session_id, trace_ids, trigger_type="message")


def _pending_cycle_trace_ids(cycle_id: str) -> list[str]:
    existing = store.get_cycle(cycle_id) or {}
    ref = str(existing.get("trigger_ref") or "")
    if ref.startswith("async:"):
        return [part for part in ref[len("async:"):].split(":") if part]
    return []
