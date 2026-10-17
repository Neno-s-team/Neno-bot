"""P3a 编排：异步消息入口、感知决策（只写作业）与周期执行。

MIND_ASYNC_DELIVERY=false 时本模块不参与 /chat；同步路径保持 P2 行为。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app import config
from app.services.mind import clock, cycle, deferred, delivery, schedule, store
from app.services.mind.schemas import L1Appraisal
from app.storage.db import fetch_all, get_recent_messages_by_tokens
from app.utils.logging_utils import log_event


def derive_cycle_id(session_id: str, key: str) -> str:
    return hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:32]


def _load_batch_text(session_id: str, trace_ids: list[str]) -> str:
    """按 trace 顺序取出消息全文并聚合。"""
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


def _open_traces(session_id: str) -> list[str]:
    """已点开且未撤回、还没进周期的感知记录（真人一次看到全部未读）。"""
    rows = fetch_all(
        """SELECT trace_id FROM mind_perception
           WHERE session_id = ? AND status = 'opened' AND seen = 'opened' AND retracted = 0
           ORDER BY received_at ASC""",
        (session_id,),
    )
    return [str(row["trace_id"]) for row in rows]


def _merge_pending_job_into_batch(session_id: str, trace_ids: list[str]) -> list[str]:
    """新消息点开时合并 pending 作业：取消作业、trace 改回 opened，并入本批。

    running 作业不动（正在生成回复，新消息单独决策、单独成作业）。
    返回合批后的完整 trace 列表。
    """
    merged = list(trace_ids)
    for job in store.list_pending_deferred(session_id):
        job_traces = [str(item) for item in json.loads(job["trace_ids_json"] or "[]")]
        # pending 合批重想：取消作业、trace 改回 opened、并入本批
        store.update_deferred(job["id"], status="cancelled")
        for item in job_traces:
            record = store.get_perception_by_trace(item)
            if record is None or record["retracted"]:
                continue
            store.update_perception(record["id"], status="opened", deferred_id=None)
            if item not in merged:
                merged.append(item)
    return merged


def _decide(
    session_id: str,
    trace_ids: list[str],
    l1: L1Appraisal,
    level: int,
) -> None:
    """回复决策。只写 mind_deferred 作业，不直接跑周期；由调度器领取执行。"""
    if l1.reply_will == "not":
        # 不回也是一种决策：perception 直接 decided，卡住恢复不会再调 L1
        for trace_id in trace_ids:
            record = store.get_perception_by_trace(trace_id)
            if record is not None and not record["retracted"]:
                store.update_perception(record["id"], status="decided")
        log_event("mind", "decision_not_reply", trace_id=trace_ids[0],
                  session_id=session_id, traces=json.dumps(trace_ids))
        return
    if level == 2:
        if l1.depth == "fast":
            _enqueue_reply_job(session_id, trace_ids, l1,
                               due_at=clock.now(), reason="level2_fast",
                               force_short=True, max_messages=2)
            return
        if l1.reply_will == "now":
            # 先短回一条；之后再等等级 >=3 深想一次
            _enqueue_reply_job(session_id, trace_ids, l1,
                               due_at=clock.now(), reason="quick_reply",
                               force_short=True, max_messages=1)
            # 等级2先短回、之后出队时按当时状态重想：短回已 ack 进历史，深想能看到
            # 但当前作业还没 done，不能复用同一条 pending（会因单 pending 合并被吞掉）。
            # 深想作业由短回作业完成后在 run_reply_job 里补写。
            return
        due = deferred.compute_due_at(clock.now(), min_level=3)
        deferred.enqueue(session_id, trace_ids, due, reason="level2_busy")
        return
    _enqueue_reply_job(session_id, trace_ids, l1,
                       due_at=clock.now(), reason="reply_now")


def _enqueue_reply_job(
    session_id: str,
    trace_ids: list[str],
    l1: L1Appraisal,
    *,
    due_at: float,
    reason: str,
    force_short: bool = False,
    max_messages: int | None = None,
) -> None:
    """决策落盘：L1 结果与 E1 约束写进作业 payload，调度器领取后执行。"""
    payload: dict[str, Any] = {"l1": l1.to_dict()}
    if force_short:
        payload["force_short"] = True
    if max_messages is not None:
        payload["max_messages"] = int(max_messages)
    job = deferred.enqueue_job(session_id, trace_ids, due_at, reason, payload)
    # enqueue_job 已统一把 perception 置 queued（绑定作业 id）


def handle_opened(session_id: str, trace_ids: list[str]) -> None:
    """点开：mark_read 立即生成（锚 seen_at，与是否回复无关），然后决策。"""
    from app.services.mind import appraisal, state as mind_state

    if not trace_ids:
        return
    # 感知失败计数：L1 抛异常时由调度器负责重试与最终 failed
    # （这里不 try/except 吞掉，让 process_glance 的调用方记录重试）
    trace_ids = _merge_pending_job_into_batch(session_id, trace_ids)
    if not trace_ids:
        return
    # 已读不回：mark_read 与回复解耦，reply_will=not 也照发
    for trace_id in trace_ids:
        record = store.get_perception_by_trace(trace_id)
        if record is None or record["retracted"] or record["seen"] != "opened":
            continue
        seen_at = float(record.get("seen_at") or clock.now())
        store.insert_delivery({
            "session_id": session_id,
            "cycle_id": f"read:{trace_id}",
            "seq": 0,
            "kind": "mark_read",
            "payload": {"trace_ids": [trace_id]},
            "send_at": seen_at,
        })

    message = _load_batch_text(session_id, trace_ids)
    if not message:
        for trace_id in trace_ids:
            record = store.get_perception_by_trace(trace_id)
            if record is not None:
                store.update_perception(record["id"], status="decided")
        return
    seg = schedule.segment_at(clock.now(), session_id=session_id)
    level = int(seg.get("level", 3))
    mind = mind_state.read_state(session_id)
    # 决策用的 L1 带自我卡和最近原话；结果进作业，run_cycle 复用不再调用
    from app.services.mind.cycle import _load_self_card

    history = get_recent_messages_by_tokens(
        session_id, token_limit=max(1000, config.HISTORY_TOKEN_LIMIT)
    )
    l1 = appraisal.run_l1(
        message=message,
        state=mind,
        self_card=_load_self_card()[:600],
        recent_turns=[item for item in history if item.get("role") in ("user", "assistant")],
        trace_id=trace_ids[0],
        session_id=session_id,
    )
    _decide(session_id, trace_ids, l1, level)


def run_reply_job(deferred_id: int) -> str | None:
    """调度器入口：带租约执行一个回复作业。成功后 perception → decided。"""
    job = store.get_deferred(deferred_id)
    if job is None or job["status"] in ("done", "cancelled", "failed"):
        return None
    if not store.lease_deferred(deferred_id, clock.now() + int(config.MIND_LEASE_SEC)):
        return None
    job = store.get_deferred(deferred_id)
    trace_ids = list(json.loads(job["trace_ids_json"] or "[]"))
    # 撤回的 trace 不进周期；全撤了就取消作业
    active_traces = []
    for item in trace_ids:
        record = store.get_perception_by_trace(item)
        if record is None or not record["retracted"]:
            active_traces.append(item)
    if not active_traces:
        store.update_deferred(deferred_id, status="cancelled")
        return None
    trace_ids = active_traces
    payload = _job_payload(job)
    if payload:
        # 决策阶段已跑过 L1 → 周期复用，不再调用
        l1_json = json.dumps(payload.get("l1") or {}, ensure_ascii=False)
    else:
        l1_json = None
    cycle_id = run_async_cycle(
        job["session_id"],
        trace_ids,
        trigger_ref=f"job:{deferred_id}",
        l1_json=l1_json,
        force_short=bool(payload.get("force_short")),
        max_messages=payload.get("max_messages"),
    )
    store.update_deferred(deferred_id, status="done", cycle_id=cycle_id)
    # 只把本作业 queue 的 perception 置 decided，不碰并发改回 opened 的行
    store.decide_queued_perceptions(deferred_id)
    if job.get("reason") == "quick_reply":
        # 短回 done 之后再补深想作业（等级 >=3）
        deep_due = deferred.compute_due_at(clock.now(), min_level=3)
        deep_job = deferred.enqueue(
            job["session_id"], trace_ids, deep_due, reason="quick_reply_then_think"
        )
        # 深想作业放宽到 decided：短回已完成，深想重新接管这批 trace。
        # 直接用 enqueue 返回的作业 id，不查 pending（同会话可能已有别的 pending 作业）。
        perception_ids = []
        for trace_id in trace_ids:
            record = store.get_perception_by_trace(trace_id)
            if record is not None and not record["retracted"]:
                perception_ids.append(int(record["id"]))
        store.queue_perceptions(
            perception_ids, int(deep_job["id"]), allow_from=("opened", "decided")
        )
    return cycle_id


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    from app.services.mind import deferred as deferred_mod

    return deferred_mod.get_payload(job)


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
    l1_json: str | None = None,
) -> str:
    """对一个聚合批跑完整认知周期并生成投递计划。"""
    if not trace_ids:
        raise ValueError("run_async_cycle requires at least one trace_id")
    message = _load_batch_text(session_id, trace_ids)
    history = get_recent_messages_by_tokens(
        session_id, token_limit=max(1000, config.HISTORY_TOKEN_LIMIT)
    )
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

    # 表情包表来自持久化的 session context（异步入口写入/沿用）
    sticker_table_text = ""
    if not injected_context:
        saved = store.get_mind_session_context(session_id)
        if saved:
            sticker_table_text = saved["sticker_table_text"]
    else:
        sticker_table_text = injected_context

    final_text = cycle.run_cycle(
        session_id,
        message,
        history,
        messages=[],
        trace_id=trace_ids[0],
        injected_context=sticker_table_text,
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        skip_l2=skip_l2,
        plan_patch=plan_patch,
        preset_l1_json=l1_json,
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
    decision_ts = float(existing.get("updated_at") or clock.now())
    delivery.plan_deliveries(
        session_id,
        final_text,
        cycle_id=cycle_id,
        base_ts=decision_ts,
        trace_ids=None,  # mark_read 已在点开时单独生成
        hesitate=hesitate,
    )
    return cycle_id


def on_new_message(session_id: str, trace_id: str) -> None:
    """新消息到达：旧周期 text 一条都没发出 → 取消剩余。

    旧 trace 并入新消息的感知批次：把旧 perception 拉回 opened 状态，
    新消息被看到时 handle_opened 会把这批 opened 一起决策成一个周期。
    不用 due=now 单独入队（那是旧行为，会产生第二个周期）。
    """
    if not config.MIND_ASYNC_DELIVERY:
        return
    for row in store.list_pending_delivery_cycles(session_id):
        cycle_id = row["cycle_id"]
        cycle_traces = set(_pending_cycle_trace_ids(cycle_id))
        # 同 session 的旧周期都属于同一对话流（单 session 部署）；
        # 有明确 trace 的周期也允许被打断并批（旧 trace 拉回 opened）。
        cycle_traces = cycle_traces or {trace_id}
        has_sent = store.cycle_has_sent_text(cycle_id)
        if has_sent:
            continue  # text/sticker 已 sent/claimed：正在发送，撤不回
        store.cancel_remaining_deliveries(cycle_id)
        for item in _pending_cycle_trace_ids(cycle_id):
            if item == trace_id:
                continue
            record = store.get_perception_by_trace(item)
            # 拉回 opened：等新消息 glance 时一起进同一个周期
            if record is not None and not record["retracted"]:
                store.update_perception(record["id"], status="opened")
        # 只取消 trace 与该周期重叠的 pending 作业，并把其 trace 改回 opened
        for job in store.list_pending_deferred(session_id):
            job_traces = [str(item) for item in json.loads(job["trace_ids_json"] or "[]")]
            if not (set(job_traces) & (cycle_traces | {trace_id})):
                continue  # 例如 quick_reply_then_think 的深想作业不动
            store.update_deferred(job["id"], status="cancelled")
            for item in job_traces:
                record = store.get_perception_by_trace(item)
                if record is not None and not record["retracted"]:
                    store.update_perception(record["id"], status="opened", deferred_id=None)
        # 未送达的意图追加一条简短经历（方案：追加，不改动原经历）
        _record_unsent(session_id, cycle_id, reason="被新消息打断")
        log_event("mind", "delivery_cycle_cancelled", session_id=session_id, cycle_id=cycle_id)


def _record_unsent(session_id: str, cycle_id: str, *, reason: str) -> None:
    """周期内 text/sticker 一条都没发出 → 追加未送达经历。幂等。"""
    if store.cycle_has_sent_text(cycle_id):
        return
    row = store.get_cycle(cycle_id) or {}
    final_text = str(row.get("final_text") or "").strip()
    if not final_text:
        return
    from app.services.mind.memory import store as memory_store

    summary = f"想回「{final_text[:40]}」但没发出去（原因：{reason}）"
    memory_store.insert_episode(
        session_id,
        cycle_id,
        note_idx=98,
        summary=summary,
        participants=["用户"],
        tags=["未送达"],
        importance=0.2,
        refs={"undelivered": True, "reason": reason},
    )


def rebuild_after_retract(session_id: str, trace_id: str) -> None:
    """撤回：只在没有任何 text/sticker 已发出时取消剩余并重建；否则只记感知。"""
    if not config.MIND_ASYNC_DELIVERY:
        return
    for row in store.list_pending_delivery_cycles(session_id):
        cycle_id = row["cycle_id"]
        trace_ids = _pending_cycle_trace_ids(cycle_id)
        if trace_id not in trace_ids:
            continue
        if store.cycle_has_sent_text(cycle_id):
            # 已发出过：不重建。撤回事件留在 mind_perception，下一轮 WM 能看到。
            log_event("mind", "retract_kept_sent_cycle",
                      session_id=session_id, cycle_id=cycle_id)
            continue
        store.cancel_remaining_deliveries(cycle_id)
        _record_unsent(session_id, cycle_id, reason="对方撤回")
        remaining = [item for item in trace_ids if item != trace_id]
        if remaining:
            deferred.enqueue(
                session_id, remaining, clock.now(), reason="retracted_rebuild"
            )
        log_event("mind", "retract_rebuilt", session_id=session_id, cycle_id=cycle_id)


def reopen_trace_in_pending_jobs(session_id: str, trace_id: str) -> None:
    """撤回的 trace 从 pending 作业里移除（store.remove_trace_from_pending_jobs 已做）。"""
    store.remove_trace_from_pending_jobs(session_id, trace_id)


def _pending_cycle_trace_ids(cycle_id: str) -> list[str]:
    existing = store.get_cycle(cycle_id) or {}
    ref = str(existing.get("trigger_ref") or "")
    if ref.startswith("job:"):
        job = store.get_deferred(int(ref.split(":", 1)[1]))
        if job:
            return list(json.loads(job["trace_ids_json"] or "[]"))
        return []
    if ref.startswith("async:"):
        return [part for part in ref[len("async:"):].split(":") if part]
    return []
