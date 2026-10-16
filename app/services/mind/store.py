from __future__ import annotations

import json
import time
from typing import Any

from app.storage.db import fetch_one, fetch_all, execute_write


def init_mind_tables():
    """兼容入口，实际建表在 db.init_db 里。"""
    from app.storage.db import init_db

    init_db()


def get_mind_state(session_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT session_id, valence, arousal, updated_at, version FROM mind_state WHERE session_id = ?",
        (session_id,),
    )
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "valence": float(row["valence"]),
        "arousal": float(row["arousal"]),
        "updated_at": float(row["updated_at"]),
        "version": int(row["version"]),
    }


def insert_mind_state(session_id: str, valence: float, arousal: float) -> int:
    """首写：已存在则不覆盖；返回受影响行数（1=插入成功，0=已存在）。"""
    return execute_write(
        """INSERT INTO mind_state(session_id, valence, arousal, updated_at, version)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(session_id) DO NOTHING""",
        (session_id, valence, arousal, time.time()),
    )


def update_mind_state_versioned(
    session_id: str,
    valence: float,
    arousal: float,
    expected_version: int,
) -> int:
    """乐观锁更新：版本不匹配时返回 0。"""
    return execute_write(
        """UPDATE mind_state
           SET valence=?, arousal=?, updated_at=?, version=version+1
           WHERE session_id=? AND version=?""",
        (valence, arousal, time.time(), session_id, expected_version),
    )


def get_mind_session_context(session_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT session_id, sticker_table_text, sticker_names_json, updated_at FROM mind_session_context WHERE session_id = ?",
        (session_id,),
    )
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "sticker_table_text": row["sticker_table_text"],
        "sticker_names_json": row["sticker_names_json"],
        "updated_at": float(row["updated_at"]),
    }


def upsert_mind_session_context(
    session_id: str, sticker_table_text: str, sticker_names_json: str
) -> None:
    now = time.time()
    execute_write(
        """INSERT INTO mind_session_context(session_id, sticker_table_text, sticker_names_json, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             sticker_table_text=excluded.sticker_table_text,
             sticker_names_json=excluded.sticker_names_json,
             updated_at=excluded.updated_at""",
        (session_id, sticker_table_text, sticker_names_json, now),
    )


def get_cycle(cycle_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT * FROM mind_cycle WHERE cycle_id = ?",
        (cycle_id,),
    )
    if row is None:
        return None
    return dict(row)


def create_cycle(
    cycle_id: str,
    session_id: str,
    trigger_type: str = "message",
    trigger_ref: str | None = None,
) -> None:
    now = time.time()
    execute_write(
        """INSERT INTO mind_cycle(cycle_id, session_id, trigger_type, trigger_ref, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'running', ?, ?)
           ON CONFLICT(cycle_id) DO NOTHING""",
        (cycle_id, session_id, trigger_type, trigger_ref, now, now),
    )


def update_cycle(
    cycle_id: str,
    *,
    status: str | None = None,
    l1_json: str | None = None,
    plan_json: str | None = None,
    expr_text: str | None = None,
    final_text: str | None = None,
    l2_json: str | None = None,
    wm_json: str | None = None,
    check_json: str | None = None,
    l2_attempts: int | None = None,
    memory_committed: int | None = None,
    degraded: str | None = None,
    models_json: str | None = None,
    error: str | None = None,
) -> None:
    now = time.time()
    fields = []
    params = []
    for key, value in [
        ("status", status),
        ("l1_json", l1_json),
        ("plan_json", plan_json),
        ("expr_text", expr_text),
        ("final_text", final_text),
        ("l2_json", l2_json),
        ("wm_json", wm_json),
        ("check_json", check_json),
        ("l2_attempts", l2_attempts),
        ("memory_committed", memory_committed),
        ("degraded", degraded),
        ("models_json", models_json),
        ("error", error),
        ("updated_at", now),
    ]:
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return
    sql = f"UPDATE mind_cycle SET {', '.join(fields)} WHERE cycle_id = ?"
    params.append(cycle_id)
    execute_write(sql, tuple(params))


def list_cycles(session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM mind_cycle WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    )
    return [dict(row) for row in rows]


# ── P3a：感知 / 待想队列 / 投递计划 ─────────────────────────────────────────

def create_perception(
    session_id: str,
    trace_id: str,
    received_at: float,
    glance_at: float,
) -> int:
    now = time.time()
    return execute_write(
        """INSERT INTO mind_perception(
               session_id, trace_id, received_at, glance_at, status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT(trace_id) DO NOTHING""",
        (session_id, trace_id, received_at, glance_at, now, now),
    )


def get_perception_by_trace(trace_id: str) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM mind_perception WHERE trace_id = ?", (trace_id,))
    return dict(row) if row is not None else None


def get_perception(perception_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM mind_perception WHERE id = ?", (perception_id,))
    return dict(row) if row is not None else None


def update_perception(
    perception_id: int,
    *,
    seen: str | None = None,
    seen_at: float | None = None,
    preview_text: str | None = None,
    l1_preview_json: str | None = None,
    retracted: int | None = None,
    retracted_seen: str | None = None,
    deferred_id: int | None = None,
    status: str | None = None,
) -> None:
    fields = []
    params: list[Any] = []
    for key, value in (
        ("seen", seen),
        ("seen_at", seen_at),
        ("preview_text", preview_text),
        ("l1_preview_json", l1_preview_json),
        ("retracted", retracted),
        ("retracted_seen", retracted_seen),
        ("deferred_id", deferred_id),
        ("status", status),
    ):
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(time.time())
    execute_write(
        f"UPDATE mind_perception SET {', '.join(fields)} WHERE id = ?",
        (*params, perception_id),
    )


def due_perceptions(ts: float, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_perception
           WHERE status = 'pending' AND retracted = 0 AND glance_at <= ?
           ORDER BY glance_at ASC LIMIT ?""",
        (ts, limit),
    )
    return [dict(row) for row in rows]


def pending_perceptions(session_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_perception
           WHERE session_id = ? AND status = 'pending' AND retracted = 0
           ORDER BY received_at ASC""",
        (session_id,),
    )
    return [dict(row) for row in rows]


def count_pending_perceptions(session_id: str, before: float) -> int:
    row = fetch_one(
        """SELECT COUNT(*) AS count FROM mind_perception
           WHERE session_id = ? AND status = 'pending' AND retracted = 0
             AND received_at < ?""",
        (session_id, before),
    )
    return int(row["count"] or 0) if row else 0


def recent_user_activity(ts: float, window_seconds: float) -> bool:
    """任一 session 在 [ts-window, ts] 内有过用户消息（P3a 单 session 部署）。"""
    row = fetch_one(
        """SELECT COUNT(*) AS count FROM messages
           WHERE role = 'user' AND created_at >= datetime(?, 'unixepoch')""",
        (ts - window_seconds,),
    )
    return bool(row and int(row["count"] or 0) > 0)


# ── 待想队列 ────────────────────────────────────────────────────────────────

def get_pending_deferred(session_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT * FROM mind_deferred WHERE session_id = ? AND status = 'pending'"
        " ORDER BY enqueued_at ASC LIMIT 1",
        (session_id,),
    )
    return dict(row) if row is not None else None


def create_deferred(
    session_id: str,
    trace_ids: list[str],
    due_at: float,
    reason: str,
) -> int:
    now = time.time()
    cursor = fetch_one("SELECT MAX(id) AS max_id FROM mind_deferred")
    next_id = int(cursor["max_id"] or 0) + 1
    execute_write(
        """INSERT INTO mind_deferred(
               id, session_id, trace_ids_json, enqueued_at, due_at, reason,
               status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (next_id, session_id, json.dumps(trace_ids, ensure_ascii=False),
         now, due_at, reason, now, now),
    )
    return next_id


def merge_deferred(deferred_id: int, trace_ids: list[str], due_at: float) -> None:
    execute_write(
        """UPDATE mind_deferred
           SET trace_ids_json = ?, due_at = MIN(due_at, ?), updated_at = ?
           WHERE id = ? AND status = 'pending'""",
        (json.dumps(trace_ids, ensure_ascii=False), due_at, time.time(), deferred_id),
    )


def get_deferred(deferred_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM mind_deferred WHERE id = ?", (deferred_id,))
    return dict(row) if row is not None else None


def update_deferred(
    deferred_id: int,
    *,
    status: str | None = None,
    lease_until: float | None = None,
    cycle_id: str | None = None,
    due_at: float | None = None,
) -> None:
    fields = []
    params: list[Any] = []
    for key, value in (
        ("status", status),
        ("lease_until", lease_until),
        ("cycle_id", cycle_id),
        ("due_at", due_at),
    ):
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(time.time())
    execute_write(
        f"UPDATE mind_deferred SET {', '.join(fields)} WHERE id = ?",
        (*params, deferred_id),
    )


def lease_deferred(deferred_id: int, lease_until: float) -> bool:
    """租约领取：pending 或租约已过期 → running。返回是否领取成功。"""
    from app.services.mind import clock

    now = clock.now()
    rowcount = execute_write(
        """UPDATE mind_deferred
           SET status = 'running', lease_until = ?, updated_at = ?
           WHERE id = ? AND (status = 'pending' OR (status = 'running' AND lease_until < ?))""",
        (lease_until, now, deferred_id, now),
    )
    return rowcount == 1


def due_deferred(ts: float, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_deferred
           WHERE status IN ('pending', 'running') AND due_at <= ?
           ORDER BY due_at ASC LIMIT ?""",
        (ts, limit),
    )
    return [dict(row) for row in rows]


# ── 投递计划 ────────────────────────────────────────────────────────────────

def insert_delivery(item: dict[str, Any]) -> None:
    execute_write(
        """INSERT INTO mind_delivery(
               session_id, cycle_id, seq, kind, payload_json, send_at, status,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT(cycle_id, seq) DO NOTHING""",
        (
            item["session_id"], item["cycle_id"], item["seq"], item["kind"],
            json.dumps(item.get("payload") or {}, ensure_ascii=False),
            item["send_at"], time.time(), time.time(),
        ),
    )


def due_deliveries(session_id: str, ts: float, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_delivery
           WHERE session_id = ? AND status = 'pending' AND send_at <= ?
           ORDER BY send_at ASC, seq ASC LIMIT ?""",
        (session_id, ts, limit),
    )
    return [dict(row) for row in rows]


def get_delivery(delivery_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM mind_delivery WHERE id = ?", (delivery_id,))
    return dict(row) if row is not None else None


def ack_delivery(delivery_id: int, status: str, *, error: str | None = None) -> bool:
    rowcount = execute_write(
        """UPDATE mind_delivery SET status = ?, acked_at = ?, error = ?, updated_at = ?
           WHERE id = ? AND status = 'pending'""",
        (status, time.time(), error, time.time(), delivery_id),
    )
    return rowcount == 1


def cancel_remaining_deliveries(cycle_id: str, after_seq: int = 0) -> int:
    """取消该周期 text/sticker 一条都没发出时剩余的全部 pending 项（含 mark_read）。"""
    return execute_write(
        """UPDATE mind_delivery SET status = 'cancelled', updated_at = ?
           WHERE cycle_id = ? AND status = 'pending'""",
        (time.time(), cycle_id),
    )


def cycle_has_sent_text(cycle_id: str) -> bool:
    row = fetch_one(
        """SELECT COUNT(*) AS count FROM mind_delivery
           WHERE cycle_id = ? AND status = 'sent' AND kind IN ('text', 'sticker')""",
        (cycle_id,),
    )
    return bool(row and int(row["count"] or 0) > 0)


def list_pending_delivery_cycles(session_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT DISTINCT cycle_id FROM mind_delivery
           WHERE session_id = ? AND status = 'pending'""",
        (session_id,),
    )
    return [dict(row) for row in rows]
