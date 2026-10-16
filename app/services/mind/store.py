from __future__ import annotations

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
