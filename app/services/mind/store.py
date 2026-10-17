from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.storage.db import fetch_one, fetch_all, execute_write
from app.services.mind import clock


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
        (session_id, valence, arousal, clock.now()),
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
        (valence, arousal, clock.now(), session_id, expected_version),
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
    now = clock.now()
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
    now = clock.now()
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
    now = clock.now()
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
    now = clock.now()
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
    attempts: int | None = None,
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
        ("attempts", attempts),
    ):
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(clock.now())
    execute_write(
        f"UPDATE mind_perception SET {', '.join(fields)} WHERE id = ?",
        (*params, perception_id),
    )


def queue_perceptions(
    perception_ids: list[int],
    deferred_id: int,
    allow_from: tuple[str, ...] = ("opened",),
) -> None:
    """perception → queued（带 deferred_id）。默认只接受 opened。

    quick_reply_then_think 场景放宽到 decided：短回已完成（decided），
    深想作业重新接管这批 trace。
    """
    if not perception_ids:
        return
    placeholders = ",".join("?" for _ in perception_ids)
    allowed = ",".join("?" for _ in allow_from)
    execute_write(
        f"""UPDATE mind_perception
            SET status='queued', deferred_id=?, updated_at=?
            WHERE id IN ({placeholders}) AND status IN ({allowed}) AND retracted=0""",
        (deferred_id, clock.now(), *perception_ids, *allow_from),
    )


def decide_queued_perceptions(deferred_id: int) -> None:
    """作业成功后：只把本作业 queue 的 perception 置 decided（不碰并发改回的 opened）。"""
    execute_write(
        """UPDATE mind_perception
           SET status='decided', updated_at=?
           WHERE deferred_id=? AND status='queued' AND retracted=0""",
        (clock.now(), deferred_id),
    )


def fail_queued_perceptions(deferred_id: int) -> None:
    execute_write(
        """UPDATE mind_perception
           SET status='failed', updated_at=?
           WHERE deferred_id=? AND status='queued'""",
        (clock.now(), deferred_id),
    )


def stuck_opened_perceptions(ts: float, grace_sec: float) -> list[dict[str, Any]]:
    """opened 但 seen_at 早于 grace、且没有 pending/running 作业 → 需要重跑 handle_opened。"""
    rows = fetch_all(
        """SELECT p.* FROM mind_perception p
           WHERE p.status='opened' AND p.retracted=0 AND p.seen_at < ?
             AND NOT EXISTS (
               SELECT 1 FROM mind_deferred d
               WHERE d.session_id=p.session_id AND d.status IN ('pending','running')
                 AND EXISTS (
                   SELECT 1 FROM json_each(d.trace_ids_json) j
                   WHERE j.value=p.trace_id
                 )
             )
           ORDER BY p.received_at ASC""",
        (ts - max(0.0, grace_sec),),
    )
    return [dict(row) for row in rows]


def increment_perception_attempts(perception_ids: list[int]) -> None:
    if not perception_ids:
        return
    placeholders = ",".join("?" for _ in perception_ids)
    execute_write(
        f"UPDATE mind_perception SET attempts=attempts+1, updated_at=? WHERE id IN ({placeholders})",
        (clock.now(), *perception_ids),
    )


def due_perceptions(ts: float, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_perception
           WHERE status IN ('pending', 'previewed') AND retracted = 0 AND glance_at <= ?
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


def open_unread_perceptions(session_id: str, seen_at: float) -> int:
    """点开聊天：同 session 已收到、未撤回的 pending/previewed 一律置 opened（同一 seen_at）。"""
    rows = fetch_all(
        """SELECT id FROM mind_perception
           WHERE session_id = ? AND status IN ('pending', 'previewed') AND retracted = 0
             AND received_at <= ?""",
        (session_id, seen_at),
    )
    for row in rows:
        update_perception(int(row["id"]), seen="opened", seen_at=seen_at, status="opened")
    return len(rows)


def count_pending_perceptions(session_id: str, before: float) -> int:
    row = fetch_one(
        """SELECT COUNT(*) AS count FROM mind_perception
           WHERE session_id = ? AND status = 'pending' AND retracted = 0
             AND received_at < ?""",
        (session_id, before),
    )
    return int(row["count"] or 0) if row else 0


def last_self_activity(session_id: str, ts: float) -> float | None:
    """本 session 在 ts 之前她最近一次活动时刻（assistant 已发出 / 她点开过）。

    - role='assistant' 的消息：异步模式下 ack sent 才写入，代表真的发出去了；
    - mind_perception.seen='opened' 的 seen_at：她点开过消息，说明她在这个聊天里。
    取两者较晚者。按 session 隔离，不看用户消息。
    """
    row = fetch_one(
        """SELECT MAX(t) AS latest FROM (
             SELECT MAX(CAST(strftime('%s', created_at) AS REAL)) AS t FROM messages
               WHERE session_id = ? AND role = 'assistant'
                     AND CAST(strftime('%s', created_at) AS REAL) < ?
             UNION ALL
             SELECT MAX(CAST(seen_at AS REAL)) AS t FROM mind_perception
               WHERE session_id = ? AND seen = 'opened' AND seen_at < ?
           )""",
        (session_id, int(ts), session_id, ts),
    )
    value = row["latest"] if row is not None else None
    return float(value) if value else None


# ── 待想队列 ────────────────────────────────────────────────────────────────

def get_pending_deferred(session_id: str) -> dict[str, Any] | None:
    row = fetch_one(
        "SELECT * FROM mind_deferred WHERE session_id = ? AND status = 'pending'"
        " ORDER BY enqueued_at ASC LIMIT 1",
        (session_id,),
    )
    return dict(row) if row is not None else None


def list_pending_deferred(session_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM mind_deferred WHERE session_id = ? AND status = 'pending'"
        " ORDER BY enqueued_at ASC",
        (session_id,),
    )
    return [dict(row) for row in rows]


def create_deferred(
    session_id: str,
    trace_ids: list[str],
    due_at: float,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> int:
    now = clock.now()
    from app.storage.db import get_conn

    with get_conn() as conn:
        cursor = conn.execute(
        """INSERT INTO mind_deferred(
               session_id, trace_ids_json, enqueued_at, due_at, reason, payload_json,
               status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (session_id, json.dumps(trace_ids, ensure_ascii=False),
             now, due_at, reason,
             json.dumps(payload or {}, ensure_ascii=False), now, now),
        )
        return int(cursor.lastrowid)


def merge_deferred(deferred_id: int, trace_ids: list[str], due_at: float) -> None:
    execute_write(
        """UPDATE mind_deferred
           SET trace_ids_json = ?, due_at = MIN(due_at, ?), updated_at = ?
           WHERE id = ? AND status = 'pending'""",
        (json.dumps(trace_ids, ensure_ascii=False), due_at, clock.now(), deferred_id),
    )


def remove_trace_from_pending_jobs(session_id: str, trace_id: str) -> None:
    """从所有 pending 作业里移除 trace；移空的作业置 cancelled。"""
    from app.storage.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, trace_ids_json FROM mind_deferred"
            " WHERE session_id=? AND status='pending'",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                trace_ids = [str(item) for item in json.loads(row["trace_ids_json"] or "[]")]
            except ValueError:
                continue
            if trace_id not in trace_ids:
                continue
            remaining = [item for item in trace_ids if item != trace_id]
            if remaining:
                conn.execute(
                    "UPDATE mind_deferred SET trace_ids_json=?, updated_at=? WHERE id=?",
                    (json.dumps(remaining, ensure_ascii=False), clock.now(), row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE mind_deferred SET status='cancelled', updated_at=? WHERE id=?",
                    (clock.now(), row["id"]),
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
    attempts: int | None = None,
) -> None:
    fields = []
    params: list[Any] = []
    for key, value in (
        ("status", status),
        ("lease_until", lease_until),
        ("cycle_id", cycle_id),
        ("due_at", due_at),
        ("attempts", attempts),
    ):
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(clock.now())
    execute_write(
        f"UPDATE mind_deferred SET {', '.join(fields)} WHERE id = ?",
        (*params, deferred_id),
    )


def lease_deferred(deferred_id: int, lease_until: float) -> bool:
    """租约领取：pending 或租约已过期的 running → running 并 +1 attempts。"""
    now = clock.now()
    rowcount = execute_write(
        """UPDATE mind_deferred
           SET status = 'running', lease_until = ?, attempts = attempts + 1, updated_at = ?
           WHERE id = ? AND (status = 'pending' OR (status = 'running' AND lease_until < ?))""",
        (lease_until, now, deferred_id, now),
    )
    return rowcount == 1


def due_deferred(ts: float, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_deferred
           WHERE (status = 'pending' AND due_at <= ?)
              OR (status = 'running' AND due_at <= ? AND lease_until < ?)
           ORDER BY due_at ASC LIMIT ?""",
        (ts, ts, ts, limit),
    )
    return [dict(row) for row in rows]


def stale_running_deferred(ts: float, limit: int = 100) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT * FROM mind_deferred
           WHERE status = 'running' AND lease_until < ?
           ORDER BY due_at ASC LIMIT ?""",
        (ts, limit),
    )
    return [dict(row) for row in rows]


# ── 投递计划 ────────────────────────────────────────────────────────────────

def insert_delivery(item: dict[str, Any]) -> None:
    now = clock.now()
    execute_write(
        """INSERT INTO mind_delivery(
               session_id, cycle_id, seq, kind, payload_json, send_at, status,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT(cycle_id, seq) DO NOTHING""",
        (
            item["session_id"], item["cycle_id"], item["seq"], item["kind"],
            json.dumps(item.get("payload") or {}, ensure_ascii=False),
            item["send_at"], now, now,
        ),
    )


def due_deliveries(session_id: str, ts: float, limit: int = 50) -> list[dict[str, Any]]:
    """原子认领：pending（或租约过期的 claimed）→ claimed + lease_until。"""
    from app import config
    from app.storage.db import get_conn

    lease_until = ts + max(1, int(config.MIND_DELIVERY_LEASE_SEC))
    # RETURNING 一条语句完成（SQLite >= 3.35）；失败则退回 BEGIN IMMEDIATE 事务
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                f"""UPDATE mind_delivery
                    SET status = 'claimed', lease_until = ?, updated_at = ?
                    WHERE id IN (
                      SELECT id FROM mind_delivery
                      WHERE session_id = ? AND send_at <= ?
                        AND (status = 'pending' OR (status = 'claimed' AND lease_until < ?))
                      ORDER BY send_at ASC, seq ASC LIMIT ?
                    )
                      AND (status = 'pending' OR (status = 'claimed' AND lease_until < ?))
                    RETURNING *""",
                (lease_until, ts, session_id, ts, ts, limit, ts),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            # SQLite < 3.35 无 RETURNING：同事务内 SELECT + 条件 UPDATE
            ids = [
                int(row["id"]) for row in conn.execute(
                    """SELECT id FROM mind_delivery
                       WHERE session_id = ? AND send_at <= ?
                         AND (status = 'pending' OR (status = 'claimed' AND lease_until < ?))
                       ORDER BY send_at ASC, seq ASC LIMIT ?""",
                    (session_id, ts, ts, limit),
                ).fetchall()
            ]
            if not ids:
                conn.execute("COMMIT")
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""UPDATE mind_delivery
                    SET status='claimed', lease_until=?, updated_at=?
                    WHERE id IN ({placeholders})
                      AND (status='pending' OR (status='claimed' AND lease_until < ?))""",
                (lease_until, ts, *ids, ts),
            )
            rows = [dict(row) for row in conn.execute(
                f"""SELECT * FROM mind_delivery
                    WHERE id IN ({placeholders}) AND status='claimed' AND lease_until=?
                    ORDER BY send_at ASC, seq ASC""",
                (*ids, lease_until),
            ).fetchall()]
        conn.execute("COMMIT")
        # RETURNING 不保证顺序；按 (send_at, seq) 排序后再返回
        return sorted(rows, key=lambda item: (item["send_at"], item["seq"]))


def get_delivery(delivery_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM mind_delivery WHERE id = ?", (delivery_id,))
    return dict(row) if row is not None else None


def ack_delivery(delivery_id: int, status: str, *, error: str | None = None) -> bool:
    now = clock.now()
    rowcount = execute_write(
        """UPDATE mind_delivery SET status = ?, acked_at = ?, error = ?, updated_at = ?
           WHERE id = ? AND status IN ('pending', 'claimed', 'cancelled')""",
        (status, now, error, now, delivery_id),
    )
    return rowcount == 1


def cancel_remaining_deliveries(cycle_id: str) -> int:
    """只取消 text/sticker/typing；mark_read 不随打断或撤回取消。"""
    return execute_write(
        """UPDATE mind_delivery SET status = 'cancelled', updated_at = ?
           WHERE cycle_id = ? AND status = 'pending'
             AND kind IN ('text', 'sticker', 'typing')""",
        (clock.now(), cycle_id),
    )


def cycle_has_sent_text(cycle_id: str) -> bool:
    """sent/claimed 的 text/sticker 都算"已发出"：claimed 已交给网关，撤不回。"""
    row = fetch_one(
        """SELECT COUNT(*) AS count FROM mind_delivery
           WHERE cycle_id = ? AND status IN ('sent', 'claimed')
             AND kind IN ('text', 'sticker')""",
        (cycle_id,),
    )
    return bool(row and int(row["count"] or 0) > 0)


def list_pending_delivery_cycles(session_id: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        """SELECT DISTINCT cycle_id FROM mind_delivery
           WHERE session_id = ? AND status IN ('pending', 'claimed')
             AND kind IN ('text', 'sticker', 'typing')""",
        (session_id,),
    )
    return [dict(row) for row in rows]
