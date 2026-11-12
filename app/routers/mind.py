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


# ── 给界面看的（只读）：她的一天、她起过的念头、一条消息背后她想的、今天花了多少钱、历史消息 ──────────

def _json(raw: Any) -> dict[str, Any]:
    import json

    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/mind/day")
def day(session_id: str, date: str | None = None):
    """她的一天（只看）：这天去了哪、干了什么、刷到了什么、谁找过她；还有睡前她自己对这天的一句话。date 不给就是今天。"""
    from datetime import datetime

    from app import config
    from app.services.mind import world_link
    from app.storage.db import fetch_one

    tz = clock.local_now().tzinfo
    try:
        local = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz) if date else clock.local_now()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = start <= clock.now() < start + 86400
    items, summary = [], []
    if session_id == config.MIND_HOME_SESSION:      # 只有接着世界的那个会话有"日子"
        items, summary = world_link.day_items(start, min(start + 86400, clock.now()), limit=200)
    # 这天的睡前回想：那一觉是这天中午之后、第二天中午之前睡下的
    night = fetch_one("SELECT result_json FROM mind_reflection WHERE session_id = ? AND status = 'done'"
                      " AND window_end >= ? AND window_end < ? ORDER BY window_end DESC LIMIT 1",
                      (session_id, start + 12 * 3600, start + 36 * 3600))
    return {"date": local.strftime("%Y-%m-%d"), "today": today, "items": items, "summary": summary,
            "diary": str(_json(night["result_json"]).get("day_summary") or "") if night else "",
            "now": now(session_id) if today else None}


@router.get("/mind/thoughts")
def thoughts(session_id: str, limit: int = 30):
    """她起过的念头，发了的和没发的都在（造物者模式的列表）。"""
    from app.services.mind import proactive

    return {"mode": proactive.mode(), "items": proactive.thoughts(session_id, max(1, min(int(limit), 100)))}


@router.get("/mind/cycles/{cycle_id}")
def inner(cycle_id: str):
    """这一轮她心里是怎么想的（造物者模式：长按她的一条消息看）。第一反应、想清楚后的感受、打算说的、没打算说的、原稿和发出去的。"""
    from app.storage.db import fetch_all

    row = store.get_cycle(cycle_id)
    if row is None:
        raise HTTPException(status_code=404, detail="cycle not found")
    l1, thought, plan = _json(row.get("l1_json")), _json(row.get("l2_json")), _json(row.get("plan_json"))
    l2, l3 = thought.get("l2") or {}, thought.get("l3") or {}
    sent = [_json(r["payload_json"]).get("text") or f"[表情包：{_json(r['payload_json']).get('name')}]"
            for r in fetch_all("SELECT payload_json FROM mind_delivery WHERE cycle_id = ? AND status = 'sent'"
                               " AND kind IN ('text', 'sticker') ORDER BY seq", (cycle_id,))]
    final = [line for line in str(row.get("final_text") or "").split("\n") if line.strip()]
    return {
        "cycle_id": cycle_id, "session_id": row["session_id"], "at": row.get("updated_at"),
        "initiative": row.get("trigger_type") == "self",                 # 是她自己想说的，不是回谁
        "gut": l1.get("gut") or "", "category": l1.get("category") or "", "depth": l1.get("depth") or "",
        "thought": {"reads": l2.get("interpretation") or "", "feels": l2.get("feeling") or ""} if l2 else None,   # 随口一回的没有深思
        "say_points": list(l3.get("say_points") or plan.get("say_points") or []),
        "hidden": list(l3.get("hidden") or []), "tone": l3.get("tone") or plan.get("tone") or "", "hesitate": bool(l3.get("hesitate")),
        "draft": str(row.get("expr_text") or ""), "final": final, "sent": sent,
        "unsent": bool(final) and not sent,                              # 想好了却没发出去（被你的新消息打断、过期）
    }


@router.get("/mind/cost")
def cost():
    """今天（本地零点起）调模型花了多少钱。提供方报了花费的用它报的，没报的按价格表估；估不出来的单独数出来。"""
    import os

    from app.storage.operations import operations

    start = clock.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = operations().all("SELECT COALESCE(provider, '') AS provider, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS usd,"
                            " SUM(cost_usd IS NULL AND status = 'ok') AS unknown FROM llm_usage WHERE created_at >= ?"
                            " GROUP BY COALESCE(provider, '')", (start,))
    usd = sum(float(r["usd"] or 0) for r in rows)
    rate = float(os.getenv("USD_CNY", "") or 7.2)
    return {"since": start, "usd": round(usd, 4), "cny": round(usd * rate, 2), "calls": sum(int(r["calls"]) for r in rows),
            "unknown_calls": sum(int(r["unknown"] or 0) for r in rows),
            "by_provider": [{"provider": r["provider"], "calls": int(r["calls"]), "usd": round(float(r["usd"] or 0), 4)} for r in rows]}


@router.get("/mind/history")
def history(session_id: str, limit: int = 60):
    """最近的聊天记录，旧的在前（界面打开时先显示这些）。你发的每条带"她看到没有"；她发的带是哪一轮想出来的。"""
    from app.services.mind import timeline
    from app.storage.db import fetch_all

    rows = fetch_all("SELECT m.id, m.role, m.content, m.message_type, m.created_at, m.trace_id, m.metadata_json, p.seen"
                     " FROM messages m LEFT JOIN mind_perception p ON p.trace_id = m.trace_id"
                     " WHERE m.session_id = ? AND m.role IN ('user', 'assistant') ORDER BY m.id DESC LIMIT ?",
                     (session_id, max(1, min(int(limit), 300))))
    items = []
    for r in reversed(rows):
        item = {"id": r["id"], "role": r["role"], "text": r["content"], "type": r["message_type"] or "text",
                "at": timeline.message_ts({"created_at": r["created_at"]})}
        if r["role"] == "user":
            item["read"] = r["seen"] == "opened"                         # 她真点开看了才算已读
        else:
            item["cycle_id"] = _json(r["metadata_json"]).get("cycle_id")
        items.append(item)
    return {"items": items}

