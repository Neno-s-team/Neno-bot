"""L0 感知与注意：看手机时刻的确定性抽样（不调模型）。

每个 trace 在收到时用 seed = sha256(session_id:trace_id) 抽样一次
"看手机时刻"（指数风险模型），之后到点（由调度器驱动）决定
seen = preview / opened，必要时进待想队列。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from app import config
from app.services.mind import clock, schedule, store
from app.services.mind.appraisal import run_l1
from app.utils.logging_utils import log_event

_MAX_SIM_HOURS = 24


def _seed_float(session_id: str, trace_id: str) -> float:
    digest = hashlib.sha256(f"{session_id}:{trace_id}".encode()).hexdigest()
    return int(digest[:16], 16) / 2**64


def _lambda(level: int, unread_count: int) -> float:
    table = config.MIND_L0_LAMBDA_PER_MIN or {}
    base = float(table.get(str(level), 0.0))
    n = max(1, unread_count)
    return base * min(2.0, 1.0 + 0.2 * (n - 1))


def sample_glance_time(received_at: float, unread_count: int, session_id: str, trace_id: str) -> float:
    """从 received_at 沿课表累加 λ·Δt，达到目标风险 H=-ln(1-u) 的时刻。"""
    u = _seed_float(session_id, trace_id)
    target = -math.log(1.0 - u)
    accumulated = 0.0
    limit = received_at + _MAX_SIM_HOURS * 3600
    for seg_start, seg_end, seg in schedule.iter_segments(received_at, limit):
        level = int(seg.get("level", 3))
        lam = _lambda(level, unread_count)
        if lam <= 0:
            continue
        span = (seg_end - seg_start) / 60.0
        accumulated += lam * span
        if accumulated >= target:
            over = (accumulated - target) / lam
            return seg_end - over * 60.0
    fallback = schedule.next_open_start(limit, min_level=2)
    return fallback if fallback is not None else limit


def create_perception(session_id: str, trace_id: str, *, received_at: float | None = None) -> dict[str, Any] | None:
    """收到消息：确定性抽样 glance_at 并落库；同 trace 重复调用复用记录。"""
    ts = clock.now() if received_at is None else received_at
    unread = store.count_pending_perceptions(session_id, ts) + 1
    glance_at = sample_glance_time(ts, unread, session_id, trace_id)
    store.create_perception(session_id, trace_id, ts, glance_at)
    record = store.get_perception_by_trace(trace_id)
    if record and record["glance_at"] != glance_at and record["status"] != "pending":
        log_event("mind", "perception_dupe_ignored", trace_id=trace_id, session_id=session_id)
    return store.get_perception_by_trace(trace_id)


def glance(session_id: str, trace_id: str) -> dict[str, Any] | None:
    """到点：等级 1 只预览（不标已读），紧急则抢占；>=2 打开（记 seen_at）。"""
    record = store.get_perception_by_trace(trace_id)
    if record is None or record["retracted"]:
        return record
    if record["status"] not in ("pending", "previewed"):
        return record
    ts = clock.now()
    # 首次 glance 按抽样的 glance_at 判等级；preview 重看时按当前时刻判
    judge_ts = record["glance_at"] if record["status"] == "pending" else ts
    seg = schedule.segment_at(judge_ts)
    level = int(seg.get("level", 3))
    if level == 1:
        preview_text = _preview_text(record)
        l1_preview = _preview_l1(session_id, record, preview_text, ts)
        seen = "preview"
        store.update_perception(
            record["id"], seen=seen, seen_at=ts, preview_text=preview_text,
            l1_preview_json=(l1_preview if isinstance(l1_preview, str) else None),
            status="glanced",
        )
        urgency = 0.0
        try:
            import json as _json

            urgency = float(_json.loads(l1_preview or "{}").get("urgency", 0.0))
        except Exception:
            urgency = 0.0
        if urgency >= float(config.MIND_PREEMPT_URGENCY):
            log_event("mind", "l0_preempt", trace_id=trace_id, session_id=session_id, urgency=urgency)
            # 抢占：当作等级 >=2，立即点开（已记 seen_at，上面已写）
            store.update_perception(record["id"], seen="opened", status="opened")
            return store.get_perception(record["id"])
        store.update_perception(record["id"], status="previewed")
        return store.get_perception(record["id"])
    store.update_perception(record["id"], seen="opened", seen_at=ts, status="opened")
    return store.get_perception(record["id"])


def _preview_text(record: dict[str, Any]) -> str:
    from app.storage.db import fetch_one

    row = fetch_one(
        "SELECT content FROM messages WHERE trace_id = ? AND role = 'user' ORDER BY id LIMIT 1",
        (record["trace_id"],),
    )
    message = row["content"] if row else ""
    return message[: max(1, int(config.MIND_PREVIEW_CHARS))]


def _preview_l1(session_id: str, record: dict[str, Any], preview_text: str, ts: float) -> str | None:
    from app.services.mind import state as mind_state_mod

    try:
        state = mind_state_mod.read_state(session_id)
        from app.services.mind.cycle import _load_self_card

        l1 = run_l1(
            message=preview_text,
            state=state,
            self_card=_load_self_card()[:600],
            recent_turns=[],
            trace_id=record["trace_id"],
            session_id=session_id,
        )
        import json as _json

        return _json.dumps(l1.to_dict(), ensure_ascii=False)
    except Exception as exc:
        log_event(
            "mind", "l0_preview_l1_failed",
            trace_id=record["trace_id"], session_id=session_id,
            error_type=type(exc).__name__,
        )
        return None
