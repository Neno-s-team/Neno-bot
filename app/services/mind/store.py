from __future__ import annotations

import time
from typing import Any

from app.storage.db import fetch_one, fetch_all, execute_write, get_conn


def init_mind_tables():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_state (
                session_id TEXT PRIMARY KEY,
                valence REAL NOT NULL DEFAULT 0.0,
                arousal REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_cycle (
                cycle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL DEFAULT 'message',
                trigger_ref TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                l1_json TEXT,
                plan_json TEXT,
                expr_text TEXT,
                final_text TEXT,
                models_json TEXT DEFAULT '{}',
                error TEXT,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mind_cycle_session ON mind_cycle(session_id, created_at)
            """
        )


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


def upsert_mind_state(session_id: str, valence: float, arousal: float, version: int = 0) -> None:
    now = time.time()
    execute_write(
        """INSERT INTO mind_state(session_id, valence, arousal, updated_at, version)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             valence=excluded.valence,
             arousal=excluded.arousal,
             updated_at=excluded.updated_at,
             version=excluded.version""",
        (session_id, valence, arousal, now, version),
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
