"""mind 模块的 P3a 接口：投递查询/ack、撤回。鉴权沿用 INTERNAL_TOKEN。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.security import require_internal_token
from app.services.mind import clock, store
from app.services.mind import orchestration
from app.utils.logging_utils import log_event

router = APIRouter(tags=["mind"], dependencies=[Depends(require_internal_token)])


class DeliveryAckRequest(BaseModel):
    status: str = Field(..., pattern="^(sent|failed)$")
    error: str | None = Field(default=None, max_length=500)


class RetractRequest(BaseModel):
    trace_id: str = Field(..., max_length=160)


def _decode_delivery(row: dict[str, Any]) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except ValueError:
        payload = {}
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "cycle_id": row["cycle_id"],
        "seq": row["seq"],
        "kind": row["kind"],
        "payload": payload,
        "send_at": row["send_at"],
        "status": row["status"],
    }


@router.get("/mind/deliveries/due")
def deliveries_due(session_id: str, limit: int = 50):
    rows = store.due_deliveries(session_id, clock.now(), limit=max(1, min(int(limit), 200)))
    return {"items": [_decode_delivery(row) for row in rows]}


@router.post("/mind/deliveries/{delivery_id}/ack")
def deliveries_ack(delivery_id: int, req: DeliveryAckRequest):
    row = store.get_delivery(delivery_id)
    if row is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    ok = store.ack_delivery(delivery_id, req.status, error=req.error)
    if not ok:
        # 重复 ack：按当前状态返回成功（幂等）
        row = store.get_delivery(delivery_id) or {}
        return {"ok": True, "id": delivery_id, "status": row.get("status"), "duplicate": True}
    if req.status == "sent":
        if row.get("status") == "cancelled":
            # 取消晚于网关实际发送：以实际发送为准
            log_event("mind", "delivery_sent_after_cancel", session_id=row["session_id"],
                      cycle_id=row["cycle_id"], candidate_id=delivery_id)
        kind = row["kind"]
        if kind in ("text", "sticker"):
            from app.storage.db import add_message

            import json

            try:
                payload = json.loads(row["payload_json"] or "{}")
            except ValueError:
                payload = {}
            content = payload.get("text") or (
                f"[表情包：{payload.get('name')}]" if kind == "sticker" else ""
            )
            if content:
                add_message(
                    row["session_id"],
                    "assistant",
                    content,
                    trace_id=f"delivery:{row['cycle_id']}:{row['seq']}",
                    message_type=kind,
                    source="mind",
                    metadata={"cycle_id": row["cycle_id"], "seq": row["seq"], "kind": kind},
                )
    else:
        log_event("mind", "delivery_failed", session_id=row["session_id"],
                  candidate_id=delivery_id, reason=req.error)
        if row["kind"] == "text":
            # text 失败：同一周期后续 text/sticker/typing 全部取消（mark_read 保留）
            store.cancel_remaining_deliveries(row["cycle_id"])
            log_event("mind", "delivery_cycle_cancelled_after_failure",
                      session_id=row["session_id"], cycle_id=row["cycle_id"])
    return {"ok": True, "id": delivery_id, "status": req.status}


@router.get("/mind/now")
def now(session_id: str):
    """她此刻在干嘛、还没看的消息大概多久后会看到。给终端通道（和以后的页面）用：让人知道她不是坏了，是还没看手机。"""
    from app.services.mind import schedule
    from app.storage.db import fetch_one

    ts = clock.now()
    seg = schedule.segment_at(ts)
    unseen = fetch_one("SELECT MIN(glance_at) AS t FROM mind_perception WHERE session_id = ? AND retracted = 0"
                       " AND status IN ('pending', 'previewed')", (session_id,))
    return {
        "activity": str(seg.get("activity") or ""), "place": str(seg.get("place") or ""),
        "phone": str(seg.get("phone") or ""), "level": int(seg.get("level", 3)), "muted": bool(seg.get("muted")),
        "sees_in": max(0, round(float(unseen["t"]) - ts)) if unseen and unseen["t"] is not None else None,
    }


@router.post("/mind/retract")
def retract(req: RetractRequest):
    record = store.get_perception_by_trace(req.trace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="perception not found")
    if record["seen"] == "none":
        # 还没看到：直接撤销，不进周期
        store.update_perception(record["id"], retracted=1, retracted_seen="none", status="cancelled")
        # 未读撤销同样从 pending 作业里移除（可能已合批）
        store.remove_trace_from_pending_jobs(record["session_id"], req.trace_id)
        return {"ok": True, "trace_id": req.trace_id, "retracted": True, "seen": "none"}
    store.update_perception(
        record["id"],
        retracted=1,
        retracted_seen=record["seen"],
        status="retracted",
    )
    # pending 作业里也去掉这条：移空就取消（周期输入不再包含撤回原文）
    store.remove_trace_from_pending_jobs(record["session_id"], req.trace_id)
    orchestration.rebuild_after_retract(record["session_id"], req.trace_id)
    return {"ok": True, "trace_id": req.trace_id, "retracted": True, "seen": record["seen"]}
