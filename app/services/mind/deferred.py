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
    """入队；已有 pending 则合并 trace_id，due_at 只能提前不能推后。"""
    existing = store.get_pending_deferred(session_id)
    if existing is not None:
        merged = list(json.loads(existing["trace_ids_json"] or "[]"))
        for trace_id in trace_ids:
            if trace_id not in merged:
                merged.append(trace_id)
        store.merge_deferred(existing["id"], merged, due_at)
        return store.get_deferred(existing["id"]) or {}
    deferred_id = store.create_deferred(session_id, trace_ids, due_at, reason)
    return store.get_deferred(deferred_id) or {}


def compute_due_at(from_ts: float, min_level: int = 2) -> float:
    """出队时刻：下一个等级 >= min_level 的段开始后再按 λ 抽一次。"""
    from app.services.mind import perception

    seg_start = schedule.next_open_start(from_ts, min_level=min_level)
    if seg_start is None:
        return from_ts + 24 * 3600
    return perception.sample_glance_time(seg_start, 1, session_id="deferred", trace_id=str(seg_start))
