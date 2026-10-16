"""待想队列：同一 session 同时只有一条 pending，新消息合并进去。"""

from __future__ import annotations

import json
from typing import Any

from app.services.mind import clock, schedule, store


def enqueue(
    session_id: str,
    trace_ids: list[str],
    due_at: float,
    reason: str,
) -> dict[str, Any]:
    """入队；已有 pending 且 reason 相同则合并，否则另立一条（如短回 done 后的深想）。"""
    existing = store.get_pending_deferred(session_id)
    if existing is not None and existing.get("reason") == reason:
        merged = list(json.loads(existing["trace_ids_json"] or "[]"))
        for trace_id in trace_ids:
            if trace_id not in merged:
                merged.append(trace_id)
        store.merge_deferred(existing["id"], merged, due_at)
        return store.get_deferred(existing["id"]) or {}
    deferred_id = store.create_deferred(session_id, trace_ids, due_at, reason)
    return store.get_deferred(deferred_id) or {}


def enqueue_job(
    session_id: str,
    trace_ids: list[str],
    due_at: float,
    reason: str,
    payload: dict | None = None,
) -> dict:
    """带 payload 的回复作业（决策结果 + E1 约束）。

    合并进已有 pending 时保留原 payload（第一次决策的 L1 仍然可用），
    新增 trace 只是扩充输入批。
    """
    existing = store.get_pending_deferred(session_id)
    if existing is not None and existing.get("reason") == reason:
        merged = list(json.loads(existing["trace_ids_json"] or "[]"))
        for trace_id in trace_ids:
            if trace_id not in merged:
                merged.append(trace_id)
        # due_at 只能提前不能推后；payload 保留首次决策结果
        store.merge_deferred(existing["id"], merged, due_at)
        return store.get_deferred(existing["id"]) or {}
    deferred_id = store.create_deferred(
        session_id, trace_ids, due_at, reason, payload=payload
    )
    return store.get_deferred(deferred_id) or {}


def get_payload(job: dict) -> dict:
    try:
        data = json.loads(job.get("payload_json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def compute_due_at(from_ts: float, min_level: int = 2) -> float:
    """出队时刻：下一个等级 >= min_level 的段开始后再按 λ 抽一次。"""
    from app.services.mind import perception

    seg_start = schedule.next_open_start(from_ts, min_level=min_level)
    if seg_start is None:
        return from_ts + 24 * 3600
    return perception.sample_glance_time(seg_start, 1, session_id="deferred", trace_id=str(seg_start))
