"""待想队列：作业各自独立；pending 合批由 handle_opened 负责。"""

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
    """入队。不同批次不再按 reason 合并；合批在 handle_opened 层完成。"""
    deferred_id = store.create_deferred(session_id, trace_ids, due_at, reason)
    return store.get_deferred(deferred_id) or {}


def enqueue_job(
    session_id: str,
    trace_ids: list[str],
    due_at: float,
    reason: str,
    payload: dict | None = None,
) -> dict:
    """带 payload 的回复作业（决策结果 + E1 约束）。不合并；合批在 handle_opened 层。"""
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
